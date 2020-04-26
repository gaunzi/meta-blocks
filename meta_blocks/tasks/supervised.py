"""Supervised tasks and task distributions for meta-learning."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import tensorflow.compat.v1 as tf

from meta_blocks.datasets import Dataset, MetaDataset
from meta_blocks.samplers.base import Sampler
from meta_blocks.tasks import base

logger = logging.getLogger(__name__)

# Transition to V2 will happen in stages.
tf.disable_v2_behavior()
tf.enable_resource_variables()

__all__ = ["SupervisedTask", "SupervisedTaskDistribution"]


class SupervisedTask(base.Task):
    """A task for supervised meta-learning.

    The Task is layer above a Dataset generated by a MetaDataset. Essentially,
    Datasets provide access to an infinite flow of data from their underlying
    Categories. Tasks manage that flow and define the following components:
        1) Query set: a balanced sample (1 per class) of labeled examples.
               Technically, this set defines the classes.
        2) (Unlabeled) support set: a (large) set of unlabeled examples.
                Basically, the rest of data that are not in the query set.
        3) (Labeled) support set: a (small) set of labeled examples.
                A few samples from the full support set that are labeled.

    Notes
    -----
    * Even though the task is supervised, the full support is unlabeled.
      The labeled support set is a subset selected from the full support set
      using internal placeholder `self._support_labeled_ids` that must be fed in
      at execution time. Note that we refer to labeled support tensors as just
      `support_tensors` and to unlabeled as `unlabeled_support_inputs`.

    * The dataset underlying a supervised task can be fairly large (e.g., mini-
      ImageNet has 600 samples per category, so that 20-way datasets have
      12,000 samples each). Pre-processing inputs in some cases might be
      computationally expensive (e.g., decoding and resizing images, applying
      optional perturbations, etc.) which is further multiplied by the size
      of the meta-batch. Most of the times, processing of the entire dataset
      is unnecessary, since learning and adaptation is performed using only
      small (labeled) support and query sets, but sometimes it may be necessary.
      Thus, the class provides a flexible access to raw and processed tensors
      that represent support and query sets.

    Parameters
    ----------
    dataset : Dataset

    num_query_shots : int, optional (default: 1)

    name: str, optional
    """

    def __init__(
        self, dataset: Dataset, num_query_shots: int = 1, name: Optional[str] = None
    ):
        super(SupervisedTask, self).__init__(
            dataset=dataset,
            num_query_shots=num_query_shots,
            name=(name or self.__class__.__name__),
        )

        # Internals.
        self._preprocessor = None
        self._support_labeled_ids = None
        self._support_inputs_raw = None
        self._support_labels_raw = None
        self._support_inputs = None
        self._support_labels = None
        self._support_tensors = None
        self._query_tensors = None

    # --- Properties. ---

    @property
    def support_size(self) -> tf.Tensor:
        """Returns the size of the (labeled) support set."""
        return tf.size(self._support_labeled_ids)

    @property
    def unlabeled_support_size(self) -> tf.Tensor:
        """Returns the size of the full unlabeled support set."""
        return tf.shape(self.unlabeled_support_inputs_raw)[0]

    @property
    def support_tensors(self) -> Tuple[tf.Tensor, tf.Tensor]:
        """Returns a tuple of `num_classes` (labeled) support input tensors.
        The input tensors are mapped through dataset's preprocessor.
        """
        return self._support_tensors

    @property
    def query_tensors(self) -> Tuple[tf.Tensor, tf.Tensor]:
        """Returns a tuple of `num_classes` query input tensors.
        The input tensors are mapped through dataset's preprocessor.
        """
        return self._query_tensors

    @property
    def unlabeled_support_inputs(self) -> tf.Tensor:
        """Returns a tensor of all unlabeled support inputs from the dataset.
        The inputs are mapped through dataset's preprocessor.
        """
        return self._support_inputs

    @property
    def unlabeled_support_inputs_raw(self) -> tf.Tensor:
        """Returns a tensor of all unlabeled support inputs from the dataset.
        The inputs are NOT preprocessed (taken as is from the dataset).
        """
        return self._support_inputs_raw

    # --- Auxiliary methods. ---

    def _get_inputs_and_labels(
        self, start_index: int, end_index: Optional[int] = None
    ) -> tf.Tensor:
        """Slices data tensors of the dataset and constructs inputs and labels.

        Note that `self.dataset` stores a tuple of `data_tensors`, where each
        tensor stores inputs of the corresponding class. This method slices
        `data_tensors`, concatenates them to construct inputs, and builds
        the corresponding labels tensor.
        """
        inputs = [x[start_index:end_index] for x in self.dataset.data_tensors]
        labels = [tf.fill(tf.shape(x)[:1], k) for k, x in enumerate(inputs)]
        return tf.concat(inputs, axis=0), tf.concat(labels, axis=0)

    def _preprocess(
        self,
        inputs: tf.Tensor,
        labels: tf.Tensor,
        back_prop: bool = False,
        parallel_iterations: int = 16,
    ) -> Tuple[tf.Tensor, tf.Tensor]:
        """Applies dataset-provided preprocessor to inputs and labels."""
        if self._preprocessor is not None:
            # TODO: determine if manual device placement is better.
            # with tf.device("CPU:0"):
            inputs = tf.map_fn(
                dtype=tf.float32,  # TODO: look up in self.dataset.
                fn=self._preprocessor,
                elems=(inputs, labels),
                back_prop=back_prop,
                parallel_iterations=parallel_iterations,
            )
        return inputs, labels

    # --- Methods. ---

    def _build(self):
        """Builds internals of the task."""
        # Input preprocessor.
        self._preprocessor = self.dataset.get_preprocessor()
        # Build a placeholder for labeled support ids.
        self._support_labeled_ids = tf.placeholder(
            tf.int32, shape=[None], name="support_labeled_ids"
        )
        # Build query tensors.
        start_index = 0
        end_index = start_index + self.num_query_shots
        inputs, labels = self._get_inputs_and_labels(start_index, end_index)
        self._query_tensors = self._preprocess(inputs, labels)
        # Build support tensors.
        start_index = end_index
        (
            self._support_inputs_raw,
            self._support_labels_raw,
        ) = self._get_inputs_and_labels(start_index, None)
        # Note: self._support_{inputs,labels} are computationally expensive to
        #       obtain for large datasets since it requires preprocessing the
        #       full support data; hence this field is not exposed through a
        #       public SupervisedTask API.
        self._support_inputs, self._support_labels = self._preprocess(
            self._support_inputs_raw, self._support_labels_raw
        )
        self._support_tensors_raw = tuple(
            map(
                lambda x: tf.gather(x, self._support_labeled_ids, axis=0),
                (self._support_inputs_raw, self._support_labels_raw),
            )
        )
        self._support_tensors = self._preprocess(*self._support_tensors_raw)

    def get_feed_list(
        self, support_labeled_ids: np.ndarray
    ) -> List[Tuple[tf.Tensor, np.ndarray]]:
        """Creates a feed list needed for the task."""
        feed_list = [(self._support_labeled_ids, support_labeled_ids)]
        return feed_list


class SupervisedTaskDistribution(base.TaskDistribution):
    """The base class for supervised task distributions.

    Parameters
    ----------
    meta_dataset : MetaDataset

    num_query_shots : int, optional (default: 1)

    num_support_shots : int, optional (default: 1)

    name: str, optional

    seed : int, optional (default: 42)
    """

    def __init__(
        self,
        meta_dataset: MetaDataset,
        num_query_shots: int = 1,
        num_support_shots: int = 1,
        name: Optional[str] = None,
        seed: Optional[int] = 42,
    ):
        super(SupervisedTaskDistribution, self).__init__(
            meta_dataset=meta_dataset,
            num_query_shots=num_query_shots,
            name=(name or self.__class__.__name__),
        )
        self.num_support_shots = num_support_shots

        # Setup random number generator.
        self._rng = np.random.RandomState(seed=seed)

        # Internals.
        self.num_requested_labels = None
        self._requests = None
        self._requested_ids = None
        self._requested_kwargs = None
        self._sampler = None

    # --- Properties. ---

    @property
    def sampler(self):
        return self._sampler

    @property
    def query_labels_per_task(self):
        return self.num_classes * self.num_query_shots

    @property
    def support_labels_per_task(self):
        return self.num_classes * self.num_support_shots

    # --- Methods. ---

    def _build(self):
        """Builds internals."""
        self._task_batch = tuple(
            SupervisedTask(
                dataset=dataset,
                num_query_shots=self.num_query_shots,
                name=f"SupervisedTask{i}",
            ).build()
            for i, dataset in enumerate(self.meta_dataset.dataset_batch)
        )

    def initialize(self, *, sampler: Sampler):
        """Initializes task distribution."""
        self._sampler = sampler

        # Reset.
        self._requests = []
        self._requested_ids = []
        self._requested_kwargs = []
        self.num_requested_labels = 0
