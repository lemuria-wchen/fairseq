# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.
#

"""
Train a network on multiple GPUs using multiprocessing.
"""

from itertools import cycle, islice
import math
import torch

from fairseq import optim, nccl, utils
from fairseq.multiprocessing_event_loop import MultiprocessingEventLoop, Future


class MultiprocessingTrainer(MultiprocessingEventLoop):
    """Main class for multi-GPU training.

    Each GPU has a full copy of the model and is assigned to its own Python
    process. Gradients are accumulated with all-reduce and all model replicas
    are updated synchronously after each batch.

    The methods in this class are divided into synchronous functions, which
    prepare and dispatch the input to each process, and asynchronous functions
    (prefixed with `_async_`), which run on each process in parallel.
    """

    def __init__(self, args, model, criterion, device_ids=None,
                 multiprocessing_method='spawn'):
        if device_ids is None:
            device_ids = tuple(range(torch.cuda.device_count()))
        super().__init__(device_ids, multiprocessing_method)

        if not torch.cuda.is_available():
            raise NotImplementedError('Training on CPU is not supported')
        model = model.share_memory()
        nccl_uid = nccl.get_unique_id()
        self.criterion = criterion

        Future.gen_list([
            self.call_async(rank, '_async_init', args=args, model=model,
                            criterion=criterion, nccl_uid=nccl_uid)
            for rank in range(self.num_replicas)
        ])

        self._grads_initialized = False

    def _async_init(self, rank, device_id, args, model, criterion, nccl_uid):
        """Initialize child processes."""
        self.args = args

        # set CUDA device
        torch.cuda.set_device(device_id)

        # initialize NCCL
        nccl.initialize(self.num_replicas, nccl_uid, device_id)

        # copy model and criterion to current device
        self.model = model.cuda()
        self.criterion = criterion.cuda()

        # initialize optimizer and LR scheduler
        self.optimizer = optim.build_optimizer(self.args, self.model.parameters())
        self.lr_scheduler = optim.lr_scheduler.build_lr_scheduler(self.args, self.optimizer)

        self.loss = None
        self._max_bsz_seen = 0
        self._num_updates = 0

    def get_model(self):
        """Get one of the model replicas."""
        # just return the first model, since all replicas are the same
        return self.call_async(0, '_async_get_model').gen()

    def _async_get_model(self, rank, device_id):
        return self.model

    def save_checkpoint(self, filename, extra_state):
        """Save a checkpoint for the current model."""
        self.call_async(0, '_async_save_checkpoint', filename=filename, extra_state=extra_state).gen()

    def _async_save_checkpoint(self, rank, device_id, filename, extra_state):
        utils.save_state(filename, self.args, self.model, self.criterion, self.optimizer,
                         self.lr_scheduler, self._num_updates, self._optim_history, extra_state)

    def load_checkpoint(self, filename):
        """Load a checkpoint into the model replicas in each process."""
        results = Future.gen_list([
            self.call_async(rank, '_async_load_checkpoint', filename=filename)
            for rank in range(self.num_replicas)
        ])
        extra_state = results[0]
        return extra_state

    def _async_load_checkpoint(self, rank, device_id, filename):
        extra_state, self._optim_history, last_optim_state = utils.load_model_state(
            filename, self.model, cuda_device=device_id)

        if last_optim_state is not None:
            # rebuild optimizer after loading model, since params may have changed
            self.optimizer = optim.build_optimizer(self.args, self.model.parameters())
            self.lr_scheduler = optim.lr_scheduler.build_lr_scheduler(self.args, self.optimizer)

            # only reload optimizer and lr_scheduler if they match
            last_optim = self._optim_history[-1]
            if last_optim['criterion_name'] == self.criterion.__class__.__name__:
                self.lr_scheduler.load_state_dict(last_optim['lr_scheduler_state'])
                if last_optim['optimizer_name'] == self.optimizer.__class__.__name__:
                    self.optimizer.load_state_dict(last_optim_state)

            self._num_updates = last_optim['num_updates']

        return extra_state

    def set_seed(self, seed):
        Future.gen_list([
            self.call_async(rank, '_async_set_seed', seed=seed)
            for rank in range(self.num_replicas)
        ])

    def _async_set_seed(self, rank, device_id, seed):
        torch.manual_seed(seed)

    def train_step(self, samples):
        """Do forward, backward and gradient step in parallel."""
        # PyTorch initializes gradient buffers lazily, so the first
        # train step needs to send non-empty samples to all replicas
        replace_empty_samples = False
        if not self._grads_initialized:
            replace_empty_samples = True
            self._grads_initialized = True

        # scatter sample across GPUs
        self._scatter_samples(samples, replace_empty_samples=replace_empty_samples)

        # forward pass
        sample_sizes, logging_outputs, ooms_fwd = Future.gen_tuple_list([
            self.call_async(rank, '_async_forward')
            for rank in range(self.num_replicas)
        ])

        # backward pass, all-reduce gradients and take an optimization step
        grad_denom = self.criterion.__class__.grad_denom(sample_sizes)
        grad_norms, ooms_bwd, lrs = Future.gen_tuple_list([
            self.call_async(rank, '_async_backward_and_opt', grad_denom=grad_denom)
            for rank in range(self.num_replicas)
        ])

        # aggregate logging output
        logging_output = self.criterion.__class__.aggregate_logging_outputs(logging_outputs)
        logging_output['lr'] = lrs[0]
        logging_output['gnorm'] = grad_norms[0]  # log the gradient norm
        logging_output['oom'] = sum(ooms_fwd) + sum(ooms_bwd)

        return logging_output

    def _async_forward(self, rank, device_id, eval=False):
        if eval:
            self.model.eval()
        else:
            self.model.train()
            self.optimizer.zero_grad()

        with utils.maybe_no_grad(eval):
            sample_size, logging_output, oom = 0, {}, False
            if self._sample is not None:
                try:
                    # calculate loss and sample size
                    self.loss, sample_size, logging_output = self.criterion(self.model, self._sample)
                except RuntimeError as e:
                    if not eval and 'out of memory' in str(e):
                        print('| WARNING: ran out of memory on GPU #{}, skipping batch'.format(device_id))
                        oom = True
                        self.loss = None
                        if hasattr(torch.cuda, 'empty_cache'):
                            torch.cuda.empty_cache()
                    else:
                        raise e

        return sample_size, logging_output, oom

    def _async_backward_and_opt(self, rank, device_id, grad_denom):
        oom = False
        if self.loss is not None:
            try:
                # backward pass
                self.loss.backward()
            except RuntimeError as e:
                if 'out of memory' in str(e):
                    print('| WARNING: ran out of memory on GPU #{}, skipping batch'.format(device_id))
                    oom = True
                    if hasattr(torch.cuda, 'empty_cache'):
                        torch.cuda.empty_cache()
                    self.optimizer.zero_grad()
                else:
                    raise e

        # all-reduce grads and rescale by grad_denom
        self._all_reduce_and_rescale_grads(grad_denom)

        # clip grads
        if self.args.clip_norm > 0:
            grad_norm = torch.nn.utils.clip_grad_norm(self.model.parameters(), self.args.clip_norm)
        else:
            grad_norm = math.sqrt(sum([p.grad.data.norm()**2 for p in self.model.parameters()]))

        # take an optimization step
        self.optimizer.step()
        self._num_updates += 1

        # update learning rate
        lr = self.lr_scheduler.step_update(self._num_updates)

        # reset loss
        self.loss = None

        return grad_norm, oom, lr

    def _all_reduce_and_rescale_grads(self, grad_denom, buffer_size=10485760):
        """All-reduce and rescale gradients in chunks of the specified size."""
        grads = [p.grad.data for p in self.model.parameters() if p.requires_grad]
        buffer_t = grads[0].new(math.ceil(buffer_size / grads[0].element_size())).zero_()
        buffer = []

        def all_reduce_buffer():
            # copy grads into buffer_t
            offset = 0
            for g in buffer:
                numel = g.numel()
                buffer_t[offset:offset+numel].copy_(g.view(-1))
                offset += numel
            # all-reduce and rescale
            nccl.all_reduce(buffer_t[:offset])
            buffer_t.div_(grad_denom)
            # copy all-reduced buffer back into grads
            offset = 0
            for g in buffer:
                numel = g.numel()
                g.view(-1).copy_(buffer_t[offset:offset+numel])
                offset += numel

        filled = 0
        for g in grads:
            sz = g.numel() * g.element_size()
            if sz > buffer_size:
                # grad is bigger than buffer, all-reduce and rescale directly
                nccl.all_reduce(g)
                g.div_(grad_denom)
            elif filled + sz > buffer_size:
                # buffer is full, all-reduce and replace buffer with grad
                all_reduce_buffer()
                buffer = [g]
                filled = sz
            else:
                # add grad to buffer
                buffer.append(g)
                filled += sz
        if len(buffer) > 0:
            all_reduce_buffer()

    def valid_step(self, samples):
        """Do forward pass in parallel."""
        # scatter sample across GPUs
        self._scatter_samples(samples, volatile=True)

        # forward pass
        _sample_sizes, logging_outputs, ooms_fwd = Future.gen_tuple_list([
            self.call_async(rank, '_async_forward', eval=True)
            for rank in range(self.num_replicas)
        ])
        assert sum(ooms_fwd) == 0

        # aggregate logging output
        logging_output = self.criterion.__class__.aggregate_logging_outputs(logging_outputs)

        return logging_output

    def get_lr(self):
        """Get the current learning rate."""
        return self.call_async(0, '_async_get_lr').gen()

    def _async_get_lr(self, rank, device_id):
        return self.optimizer.get_lr()

    def lr_step(self, epoch, val_loss=None):
        """Adjust the learning rate based on the validation loss."""
        lr = Future.gen_list([
            self.call_async(rank, '_async_lr_step', epoch=epoch, val_loss=val_loss)
            for rank in range(self.num_replicas)
        ])
        return lr[0]

    def _async_lr_step(self, rank, device_id, epoch, val_loss):
        return self.lr_scheduler.step(epoch, val_loss)

    def get_num_updates(self):
        """Get the number of parameters updates."""
        return self.call_async(0, '_async_get_num_updates').gen()

    def _async_get_num_updates(self, rank, device_id):
        return self._num_updates

    def _scatter_samples(self, samples, volatile=False, replace_empty_samples=False):
        """Split and distribute a sample across GPUs."""
        if not replace_empty_samples:
            # pad with None until its size is equal to the number of replicas
            samples = samples + [None]*(self.num_replicas - len(samples))
        else:
            # pad by cycling through the given samples
            samples = list(islice(cycle(samples), self.num_replicas))

        Future.gen_list([
            self.call_async(rank, '_async_prepare_sample', sample=samples[rank], volatile=volatile)
            for rank in range(self.num_replicas)
        ])

    def _async_prepare_sample(self, rank, device_id, sample, volatile):
        if sample is None:
            self._sample = None
        else:
            if hasattr(torch.cuda, 'empty_cache'):
                # clear the caching allocator if this is the largest sample we've seen
                if sample['target'].size(0) > self._max_bsz_seen:
                    self._max_bsz_seen = sample['target'].size(0)
                    torch.cuda.empty_cache()

            self._sample = utils.make_variable(sample, volatile=volatile, cuda_device=device_id)
