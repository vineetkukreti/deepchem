"""Model-Agnostic Meta-Learning (MAML) algorithm for low data learning."""

import os
import shutil
import tempfile
import time

import torch

from deepchem.models.optimizers import Adam, GradientDescent, LearningRateSchedule
from typing import Optional


class TorchMetaLearner(object):
    """Model and data to which the MAML algorithm can be applied.

    To use MAML, create a subclass of this defining the learning problem to solve.
    It consists of a model that can be trained to perform many different tasks, and
    data for training it on a large (possibly infinite) set of different tasks.
    """

    def compute_model(self, inputs, variables, training):
        """Compute the model for a set of inputs and variables.

        Parameters
        ----------
        inputs: list of tensors
            the inputs to the model
        variables: list of tensors
            the values to use for the model's variables.  This might be the actual
            variables (as returned by the MetaLearner's variables property), or
            alternatively it might be the values of those variables after one or more
            steps of gradient descent for the current task.
        training: bool
            indicates whether the model is being invoked for training or prediction

        Returns
        -------
        (loss, outputs) where loss is the value of the model's loss function, and
        outputs is a list of the model's outputs
        """
        raise NotImplementedError("Subclasses must implement this")

    @property
    def variables(self):
        """Get the list of variables to train."""
        raise NotImplementedError("Subclasses must implement this")

    def select_task(self):
        """Select a new task to train on.

        If there is a fixed set of training tasks, this will typically cycle through them.
        If there are infinitely many training tasks, this can simply select a new one each
        time it is called.
        """
        raise NotImplementedError("Subclasses must implement this")

    def get_batch(self):
        """Get a batch of data for training.

        This should return the data as a list of arrays, one for each of the model's
        inputs.  This will usually be called twice for each task, and should
        return a different batch on each call.
        """
        raise NotImplementedError("Subclasses must implement this")

    def parameters(self):
        """Get the parameters to be passed to the optimizer."""
        raise NotImplementedError("Subclasses must implement this")


class TorchMAML(object):
    """Implements the Model-Agnostic Meta-Learning algorithm for low data learning.

    The algorithm is described in Finn et al., "Model-Agnostic Meta-Learning for Fast
    Adaptation of Deep Networks" (https://arxiv.org/abs/1703.03400).  It is used for
    training models that can perform a variety of tasks, depending on what data they
    are trained on.  It assumes you have training data for many tasks, but only a small
    amount for each one.  It performs "meta-learning" by looping over tasks and trying
    to minimize the loss on each one *after* one or a few steps of gradient descent.
    That is, it does not try to create a model that can directly solve the tasks, but
    rather tries to create a model that is very easy to train.

    To use this class, create a subclass of MetaLearner that encapsulates the model
    and data for your learning problem.  Pass it to a MAML object and call fit().
    You can then use train_on_current_task() to fine tune the model for a particular
    task.
    """

    def __init__(
        self,
        learner,
        learning_rate=0.001,
        optimization_steps=1,
        meta_batch_size=10,
        optimizer=Adam(),
        model_dir=None,
        device: Optional[torch.device] = None,
    ):
        """Create an object for performing meta-optimization.

        Parameters
        ----------
        learner: MetaLearner
            defines the meta-learning problem
        learning_rate: float or Tensor
            the learning rate to use for optimizing each task (not to be confused with the one used
            for meta-learning).  This can optionally be made a variable (represented as a
            Tensor), in which case the learning rate will itself be learnable.
        optimization_steps: int
            the number of steps of gradient descent to perform for each task
        meta_batch_size: int
            the number of tasks to use for each step of meta-learning
        optimizer: Optimizer
            the optimizer to use for meta-learning (not to be confused with the gradient descent
            optimization performed for each task)
        model_dir: str
            the directory in which the model will be saved.  If None, a temporary directory will be created.
        device: torch.device, optional (default None)
            the device on which to run computations.  If None, a device is
            chosen automatically.
        """
        # Record inputs.

        self.learner = learner
        self.learning_rate: float = learning_rate
        self.optimization_steps: int = optimization_steps
        self.meta_batch_size: int = meta_batch_size
        self.optimizer = optimizer

        # Create the output directory if necessary.

        self._model_dir_is_temp: bool = False
        if model_dir is not None:
            if not os.path.exists(model_dir):
                os.makedirs(model_dir)
        else:
            model_dir = tempfile.mkdtemp()
            self._model_dir_is_temp = True
        self.model_dir = model_dir
        self.save_file = "%s/%s" % (self.model_dir, "model")

        # Select a device.

        if device is None:
            if torch.cuda.is_available():
                device = torch.device('cuda')
            elif torch.backends.mps.is_available():
                device = torch.device('mps')
            else:
                device = torch.device('cpu')
        self.device = device
        self.learner.w1 = self.learner.w1.to(device)
        self.learner.b1 = self.learner.b1.to(device)
        self.learner.w2 = self.learner.w2.to(device)
        self.learner.b2 = self.learner.b2.to(device)
        self.learner.w3 = self.learner.w3.to(device)
        self.learner.b3 = self.learner.b3.to(device)

        # Create the optimizers for meta-optimization and task optimization.

        self._global_step = 0
        self._pytorch_optimizer = self.optimizer._create_pytorch_optimizer(
            self.learner.parameters())
        if isinstance(self.optimizer.learning_rate, LearningRateSchedule):
            self._lr_schedule = self.optimizer.learning_rate._create_pytorch_schedule(
                self._pytorch_optimizer)
        else:
            self._lr_schedule = None

        task_optimizer = GradientDescent(learning_rate=self.learning_rate)
        self._pytorch_task_optimizer = task_optimizer._create_pytorch_optimizer(
            self.learner.parameters())
        if isinstance(task_optimizer.learning_rate, LearningRateSchedule):
            self._lr_schedule = task_optimizer.learning_rate._create_pytorch_schedule(
                self._pytorch_task_optimizer)
        else:
            self._lr_schedule = None

    def __del__(self):
        if '_model_dir_is_temp' in dir(self) and self._model_dir_is_temp:
            shutil.rmtree(self.model_dir)

    def fit(self,
            steps,
            max_checkpoints_to_keep=5,
            checkpoint_interval=600,
            restore=False):
        """Perform meta-learning to train the model.

        Parameters
        ----------
        steps: int
            the number of steps of meta-learning to perform
        max_checkpoints_to_keep: int
            the maximum number of checkpoint files to keep.  When this number is reached, older
            files are deleted.
        checkpoint_interval: float
            the time interval at which to save checkpoints, measured in seconds
        restore: bool
            if True, restore the model from the most recent checkpoint before training
            it further
        """
        if restore:
            self.restore()
        checkpoint_time = time.time()

        # Main optimization loop.

        learner = self.learner
        variables = learner.variables
        for i in range(steps):
            self._pytorch_optimizer.zero_grad()
            for j in range(self.meta_batch_size):
                learner.select_task()
                meta_loss, meta_gradients = self._compute_meta_loss(
                    learner.get_batch(), learner.get_batch(), variables)
                if j == 0:
                    summed_gradients = meta_gradients
                else:
                    summed_gradients = [
                        s + g for s, g in zip(summed_gradients, meta_gradients)
                    ]
            self._pytorch_optimizer.step()

            # Do checkpointing.

            if i == steps - 1 or time.time(
            ) >= checkpoint_time + checkpoint_interval:
                self.save_checkpoint(max_checkpoints_to_keep)
                checkpoint_time = time.time()

    def _compute_meta_loss(self, inputs, inputs2, variables):
        """This is called during fitting to compute the meta-loss (the loss after a
        few steps of optimization), and its gradient.
        """
        updated_variables = variables
        for k in range(self.optimization_steps):
            gradients = []
            loss, _ = self.learner.compute_model(inputs, updated_variables,
                                                 True)
            loss.backward()
            gradients = [i.grad.clone() for i in updated_variables]
            updated_variables = [
                v if g is None else v - self.learning_rate * g
                for v, g in zip(updated_variables, gradients)
            ]
        meta_loss, _ = self.learner.compute_model(inputs2, updated_variables,
                                                  True)
        meta_loss.backward()
        meta_gradients = [j.grad.clone() for j in variables]
        return meta_loss, meta_gradients

    def restore(self):
        """Reload the model parameters from the most recent checkpoint file."""
        last_checkpoint = sorted(self.get_checkpoints(self.model_dir))
        if len(last_checkpoint) == 0:
            raise ValueError('No checkpoint found')
        last_checkpoint = last_checkpoint[0]
        data = torch.load(last_checkpoint, map_location=self.device)
        self.learner.__dict__ = data['model_state_dict']
        self._pytorch_optimizer.load_state_dict(data['optimizer_state_dict'])
        self._pytorch_task_optimizer.load_state_dict(
            data['task_optimizer_state_dict'])
        self._global_step = data['global_step']

    def train_on_current_task(self, optimization_steps=1, restore=True):
        """Perform a few steps of gradient descent to fine tune the model on the current task.

        Parameters
        ----------
        optimization_steps: int
            the number of steps of gradient descent to perform
        restore: bool
            if True, restore the model from the most recent checkpoint before optimizing
        """
        if restore:
            self.restore()
        variables = self.learner.variables
        for i in range(optimization_steps):
            self._pytorch_task_optimizer.zero_grad()
            inputs = self.learner.get_batch()
            loss, _ = self.learner.compute_model(inputs, variables, True)
            loss.backward()
            # gradients = [j.grad.clone() for j in variables]
            self._pytorch_task_optimizer.step()

    def predict_on_batch(self, inputs):
        """Compute the model's outputs for a batch of inputs.

        Parameters
        ----------
        inputs: list of arrays
            the inputs to the model

        Returns
        -------
        (loss, outputs) where loss is the value of the model's loss function, and
        outputs is a list of the model's outputs
        """
        return self.learner.compute_model(inputs, self.learner.variables, False)

    def save_checkpoint(self,
                        max_checkpoints_to_keep: int = 5,
                        model_dir: Optional[str] = None) -> None:
        """Save a checkpoint to disk.

        Usually you do not need to call this method, since fit() saves checkpoints
        automatically.  If you have disabled automatic checkpointing during fitting,
        this can be called to manually write checkpoints.

        Parameters
        ----------
        max_checkpoints_to_keep: int
            the maximum number of checkpoints to keep.  Older checkpoints are discarded.
        model_dir: str, default None
            Model directory to save checkpoint to. If None, revert to self.model_dir
        """
        if model_dir is None:
            model_dir = self.model_dir
        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        # Save the checkpoint to a file.

        data = {
            'model_state_dict':
                self.learner.__dict__,
            'optimizer_state_dict':
                self._pytorch_optimizer.state_dict(),
            'task_optimizer_state_dict':
                self._pytorch_task_optimizer.state_dict(),
            'global_step':
                self._global_step
        }
        temp_file = os.path.join(model_dir, 'temp_checkpoint.pt')
        torch.save(data, temp_file)

        # Rename and delete older files.

        paths = [
            os.path.join(model_dir, 'checkpoint%d.pt' % (i + 1))
            for i in range(max_checkpoints_to_keep)
        ]
        if os.path.exists(paths[-1]):
            os.remove(paths[-1])
        for i in reversed(range(max_checkpoints_to_keep - 1)):
            if os.path.exists(paths[i]):
                os.rename(paths[i], paths[i + 1])
        os.rename(temp_file, paths[0])

    def get_checkpoints(self, model_dir: Optional[str] = None):
        """Get a list of all available checkpoint files.

        Parameters
        ----------
        model_dir: str, default None
            Directory to get list of checkpoints from. Reverts to self.model_dir if None

        """
        if model_dir is None:
            model_dir = self.model_dir
        files = sorted(os.listdir(model_dir))
        files = [
            f for f in files if f.startswith('checkpoint') and f.endswith('.pt')
        ]
        return [os.path.join(model_dir, f) for f in files]
