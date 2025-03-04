"""
Detection evaluation.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import itertools
import logging

import numpy as np

import fiftyone.core.evaluation as foe
import fiftyone.core.fields as fof
import fiftyone.core.labels as fol
import fiftyone.core.utils as fou
import fiftyone.core.validation as fov

from .base import BaseEvaluationResults
from .utils import compute_ious


logger = logging.getLogger(__name__)


def evaluate_detections(
    samples,
    pred_field,
    gt_field="ground_truth",
    eval_key=None,
    classes=None,
    missing=None,
    method="coco",
    iou=0.50,
    use_masks=False,
    use_boxes=False,
    classwise=True,
    **kwargs,
):
    """Evaluates the predicted detections in the given samples with respect to
    the specified ground truth detections.

    This method supports evaluating the following spatial data types:

    -   Object detections in :class:`fiftyone.core.labels.Detections` format
    -   Instance segmentations in :class:`fiftyone.core.labels.Detections`
        format with their ``mask`` attributes populated
    -   Polygons in :class:`fiftyone.core.labels.Polylines` format

    By default, this method uses COCO-style evaluation, but you can use the
    ``method`` parameter to select a different method, and you can optionally
    customize the method by passing additional parameters for the method's
    config class as ``kwargs``.

    The supported ``method`` values and their associated configs are:

    -   ``"coco"``: :class:`fiftyone.utils.eval.coco.COCOEvaluationConfig`
    -   ``"open-images"``: :class:`fiftyone.utils.eval.openimages.OpenImagesEvaluationConfig`

    If an ``eval_key`` is provided, a number of fields are populated at the
    object- and sample-level recording the results of the evaluation:

    -   True positive (TP), false positive (FP), and false negative (FN) counts
        for the each sample are saved in top-level fields of each sample::

            TP: sample.<eval_key>_tp
            FP: sample.<eval_key>_fp
            FN: sample.<eval_key>_fn

        In addition, when evaluating frame-level objects, TP/FP/FN counts are
        recorded for each frame::

            TP: frame.<eval_key>_tp
            FP: frame.<eval_key>_fp
            FN: frame.<eval_key>_fn

    -   The fields listed below are populated on each individual object; these
        fields tabulate the TP/FP/FN status of the object, the ID of the
        matching object (if any), and the matching IoU::

            TP/FP/FN: object.<eval_key>
                  ID: object.<eval_key>_id
                 IoU: object.<eval_key>_iou

    Args:
        samples: a :class:`fiftyone.core.collections.SampleCollection`
        pred_field: the name of the field containing the predicted
            :class:`fiftyone.core.labels.Detections` or
            :class:`fiftyone.core.labels.Polylines`
        gt_field ("ground_truth"): the name of the field containing the ground
            truth :class:`fiftyone.core.labels.Detections` or
            :class:`fiftyone.core.labels.Polylines`
        eval_key (None): an evaluation key to use to refer to this evaluation
        classes (None): the list of possible classes. If not provided, classes
            are loaded from :meth:`fiftyone.core.dataset.Dataset.classes` or
            :meth:`fiftyone.core.dataset.Dataset.default_classes` if
            possible, or else the observed ground truth/predicted labels are
            used
        missing (None): a missing label string. Any unmatched objects are given
            this label for results purposes
        method ("coco"): a string specifying the evaluation method to use.
            Supported values are ``("coco", "open-images")``
        iou (0.50): the IoU threshold to use to determine matches
        use_masks (False): whether to compute IoUs using the instances masks in
            the ``mask`` attribute of the provided objects, which must be
            :class:`fiftyone.core.labels.Detection` instances
        use_boxes (False): whether to compute IoUs using the bounding boxes
            of the provided :class:`fiftyone.core.labels.Polyline` instances
            rather than using their actual geometries
        classwise (True): whether to only match objects with the same class
            label (True) or allow matches between classes (False)
        **kwargs: optional keyword arguments for the constructor of the
            :class:`DetectionEvaluationConfig` being used

    Returns:
        a :class:`DetectionResults`
    """
    fov.validate_collection_label_fields(
        samples,
        (pred_field, gt_field),
        (fol.Detections, fol.Polylines),
        same_type=True,
    )

    if classes is None:
        if pred_field in samples.classes:
            classes = samples.classes[pred_field]
        elif gt_field in samples.classes:
            classes = samples.classes[gt_field]
        elif samples.default_classes:
            classes = samples.default_classes

    config = _parse_config(
        pred_field,
        gt_field,
        method,
        iou=iou,
        use_masks=use_masks,
        use_boxes=use_boxes,
        classwise=classwise,
        **kwargs,
    )
    eval_method = config.build()
    eval_method.register_run(samples, eval_key)
    eval_method.register_samples(samples)

    if not config.requires_additional_fields:
        _samples = samples.select_fields([gt_field, pred_field])
    else:
        _samples = samples

    processing_frames = samples._is_frame_field(pred_field)

    if eval_key is not None:
        tp_field = "%s_tp" % eval_key
        fp_field = "%s_fp" % eval_key
        fn_field = "%s_fn" % eval_key

        # note: fields are manually declared so they'll exist even when
        # `samples` is empty
        dataset = samples._dataset
        dataset._add_sample_field_if_necessary(tp_field, fof.IntField)
        dataset._add_sample_field_if_necessary(fp_field, fof.IntField)
        dataset._add_sample_field_if_necessary(fn_field, fof.IntField)
        if processing_frames:
            dataset._add_frame_field_if_necessary(tp_field, fof.IntField)
            dataset._add_frame_field_if_necessary(fp_field, fof.IntField)
            dataset._add_frame_field_if_necessary(fn_field, fof.IntField)

    matches = []
    logger.info("Evaluating detections...")
    for sample in _samples.iter_samples(progress=True):
        if processing_frames:
            images = sample.frames.values()
        else:
            images = [sample]

        sample_tp = 0
        sample_fp = 0
        sample_fn = 0
        for image in images:
            image_matches = eval_method.evaluate_image(
                image, eval_key=eval_key
            )
            matches.extend(image_matches)
            tp, fp, fn = _tally_matches(image_matches)
            sample_tp += tp
            sample_fp += fp
            sample_fn += fn

            if processing_frames and eval_key is not None:
                image[tp_field] = tp
                image[fp_field] = fp
                image[fn_field] = fn

        if eval_key is not None:
            sample[tp_field] = sample_tp
            sample[fp_field] = sample_fp
            sample[fn_field] = sample_fn
            sample.save()

    results = eval_method.generate_results(
        samples, matches, eval_key=eval_key, classes=classes, missing=missing
    )
    eval_method.save_run_results(samples, eval_key, results)

    return results


def compute_max_ious(
    sample_collection, label_field, attr_name="max_iou", **kwargs
):
    """Populates an attribute on each label in the given spatial field that
    records the max IoU between the object and another object in the same
    sample/frame.

    Args:
        sample_collection: a
            :class:`fiftyone.core.collections.SampleCollection`
        label_field: a label field of type
            :class:`fiftyone.core.labels.Detections` or
            :class:`fiftyone.core.labels.Polylines`
        attr_name ("max_iou"): the name of the label attribute to populate
        **kwargs: optional keyword arguments for
            :meth:`fiftyone.utils.eval.utils.compute_ious`
    """
    fov.validate_collection_label_fields(
        sample_collection, label_field, (fol.Detections, fol.Polylines)
    )

    is_frame_field = sample_collection._is_frame_field(label_field)
    _, labels_path = sample_collection._get_label_field_path(label_field)
    _, iou_path = sample_collection._get_label_field_path(
        label_field, attr_name
    )

    all_labels = sample_collection.values(labels_path)

    max_ious = []
    with fou.ProgressBar(total=len(all_labels)) as pb:
        for labels in pb(all_labels):
            if labels is None:
                sample_ious = None
            elif is_frame_field:
                sample_ious = [_compute_max_ious(l, **kwargs) for l in labels]
            else:
                sample_ious = _compute_max_ious(labels, **kwargs)

            max_ious.append(sample_ious)

    sample_collection.set_values(iou_path, max_ious)


def _compute_max_ious(labels, **kwargs):
    if labels is None:
        return None

    if len(labels) < 2:
        return [0] * len(labels)

    all_ious = compute_ious(labels, labels, **kwargs)
    np.fill_diagonal(all_ious, 0)  # exclude self
    return list(all_ious.max(axis=1))


class DetectionEvaluationConfig(foe.EvaluationMethodConfig):
    """Base class for configuring :class:`DetectionEvaluation` instances.

    Args:
        pred_field: the name of the field containing the predicted
            :class:`fiftyone.core.labels.Detections` or
            :class:`fiftyone.core.labels.Polylines`
        gt_field: the name of the field containing the ground truth
            :class:`fiftyone.core.labels.Detections` or
            :class:`fiftyone.core.labels.Polylines`
        iou (None): the IoU threshold to use to determine matches
        classwise (None): whether to only match objects with the same class
            label (True) or allow matches between classes (False)
    """

    def __init__(
        self, pred_field, gt_field, iou=None, classwise=None, **kwargs
    ):
        super().__init__(**kwargs)
        self.pred_field = pred_field
        self.gt_field = gt_field
        self.iou = iou
        self.classwise = classwise

    @property
    def requires_additional_fields(self):
        """Whether fields besides ``pred_field`` and ``gt_field`` are required
        in order to perform evaluation.

        If True then the entire samples will be loaded rather than using
        :meth:`select_fields() <fiftyone.core.collections.SampleCollection.select_fields>`
        to optimize.
        """
        return False


class DetectionEvaluation(foe.EvaluationMethod):
    """Base class for detection evaluation methods.

    Args:
        config: a :class:`DetectionEvaluationConfig`
    """

    def __init__(self, config):
        super().__init__(config)
        self.gt_field = None
        self.pred_field = None

    def register_samples(self, samples):
        """Registers the sample collection on which evaluation will be
        performed.

        This method will be called before the first call to
        :meth:`evaluate_image`. Subclasses can extend this method to perform
        any setup required for an evaluation run.

        Args:
            samples: a :class:`fiftyone.core.collections.SampleCollection`
        """
        self.gt_field, _ = samples._handle_frame_field(self.config.gt_field)
        self.pred_field, _ = samples._handle_frame_field(
            self.config.pred_field
        )

    def evaluate_image(self, sample_or_frame, eval_key=None):
        """Evaluates the ground truth and predicted objects in an image.

        Args:
            sample_or_frame: a :class:`fiftyone.core.Sample` or
                :class:`fiftyone.core.frame.Frame`
            eval_key (None): the evaluation key for this evaluation

        Returns:
            a list of matched ``(gt_label, pred_label, iou, pred_confidence)``
            tuples
        """
        raise NotImplementedError("subclass must implement evaluate_image()")

    def generate_results(
        self, samples, matches, eval_key=None, classes=None, missing=None
    ):
        """Generates aggregate evaluation results for the samples.

        Subclasses may perform additional computations here such as IoU sweeps
        in order to generate mAP, PR curves, etc.

        Args:
            samples: a :class:`fiftyone.core.collections.SampleCollection`
            matches: a list of
                ``(gt_label, pred_label, iou, pred_confidence, gt_id, pred_id)``
                matches. Either label can be ``None`` to indicate an unmatched
                object
            eval_key (None): the evaluation key for this evaluation
            classes (None): the list of possible classes. If not provided, the
                observed ground truth/predicted labels are used for results
                purposes
            missing (None): a missing label string. Any unmatched objects are
                given this label for results purposes

        Returns:
            a :class:`DetectionResults`
        """
        return DetectionResults(
            matches,
            eval_key=eval_key,
            gt_field=self.config.gt_field,
            pred_field=self.config.pred_field,
            classes=classes,
            missing=missing,
            samples=samples,
        )

    def get_fields(self, samples, eval_key):
        pred_field = self.config.pred_field
        pred_type = samples._get_label_field_type(pred_field)
        pred_key = "%s.%s.%s" % (
            pred_field,
            pred_type._LABEL_LIST_FIELD,
            eval_key,
        )

        gt_field = self.config.gt_field
        gt_type = samples._get_label_field_type(gt_field)
        gt_key = "%s.%s.%s" % (gt_field, gt_type._LABEL_LIST_FIELD, eval_key)

        fields = [
            "%s_tp" % eval_key,
            "%s_fp" % eval_key,
            "%s_fn" % eval_key,
            pred_key,
            "%s_id" % pred_key,
            "%s_iou" % pred_key,
            gt_key,
            "%s_id" % gt_key,
            "%s_iou" % gt_key,
        ]

        if samples._is_frame_field(gt_field):
            prefix = samples._FRAMES_PREFIX + eval_key
            fields.extend(
                ["%s_tp" % prefix, "%s_fp" % prefix, "%s_fn" % prefix]
            )

        return fields

    def cleanup(self, samples, eval_key):
        fields = [
            "%s_tp" % eval_key,
            "%s_fp" % eval_key,
            "%s_fn" % eval_key,
        ]

        try:
            pred_field, _ = samples._handle_frame_field(self.config.pred_field)
            pred_type = samples._get_label_field_type(self.config.pred_field)
            pred_key = "%s.%s.%s" % (
                pred_field,
                pred_type._LABEL_LIST_FIELD,
                eval_key,
            )
            fields.extend([pred_key, "%s_id" % pred_key, "%s_iou" % pred_key])
        except ValueError:
            # Field no longer exists, nothing to cleanup
            pass

        try:
            gt_field, _ = samples._handle_frame_field(self.config.gt_field)
            gt_type = samples._get_label_field_type(self.config.gt_field)
            gt_key = "%s.%s.%s" % (
                gt_field,
                gt_type._LABEL_LIST_FIELD,
                eval_key,
            )
            fields.extend([gt_key, "%s_id" % gt_key, "%s_iou" % gt_key])
        except ValueError:
            # Field no longer exists, nothing to cleanup
            pass

        if samples._is_frame_field(self.config.pred_field):
            samples._dataset.delete_sample_fields(
                ["%s_tp" % eval_key, "%s_fp" % eval_key, "%s_fn" % eval_key],
                error_level=1,
            )
            samples._dataset.delete_frame_fields(fields, error_level=1)
        else:
            samples._dataset.delete_sample_fields(fields, error_level=1)

    def _validate_run(self, samples, eval_key, existing_info):
        self._validate_fields_match(eval_key, "pred_field", existing_info)
        self._validate_fields_match(eval_key, "gt_field", existing_info)


class DetectionResults(BaseEvaluationResults):
    """Class that stores the results of a detection evaluation.

    Args:
        matches: a list of
            ``(gt_label, pred_label, iou, pred_confidence, gt_id, pred_id)``
            matches. Either label can be ``None`` to indicate an unmatched
            object
        eval_key (None): the evaluation key for this evaluation
        gt_field (None): the name of the ground truth field
        pred_field (None): the name of the predictions field
        classes (None): the list of possible classes. If not provided, the
            observed ground truth/predicted labels are used
        missing (None): a missing label string. Any unmatched objects are given
            this label for evaluation purposes
        samples (None): the :class:`fiftyone.core.collections.SampleCollection`
            for which the results were computed
    """

    def __init__(
        self,
        matches,
        eval_key=None,
        gt_field=None,
        pred_field=None,
        classes=None,
        missing=None,
        samples=None,
    ):
        if matches:
            ytrue, ypred, ious, confs, ytrue_ids, ypred_ids = zip(*matches)
        else:
            ytrue, ypred, ious, confs, ytrue_ids, ypred_ids = (
                [],
                [],
                [],
                [],
                [],
                [],
            )

        super().__init__(
            ytrue,
            ypred,
            confs=confs,
            eval_key=eval_key,
            gt_field=gt_field,
            pred_field=pred_field,
            ytrue_ids=ytrue_ids,
            ypred_ids=ypred_ids,
            classes=classes,
            missing=missing,
            samples=samples,
        )
        self.ious = np.array(ious)

    @classmethod
    def _from_dict(cls, d, samples, config, **kwargs):
        ytrue = d["ytrue"]
        ypred = d["ypred"]
        ious = d["ious"]

        confs = d.get("confs", None)
        if confs is None:
            confs = itertools.repeat(None)

        ytrue_ids = d.get("ytrue_ids", None)
        if ytrue_ids is None:
            ytrue_ids = itertools.repeat(None)

        ypred_ids = d.get("ypred_ids", None)
        if ypred_ids is None:
            ypred_ids = itertools.repeat(None)

        eval_key = d.get("eval_key", None)
        gt_field = d.get("gt_field", None)
        pred_field = d.get("pred_field", None)
        classes = d.get("classes", None)
        missing = d.get("missing", None)

        matches = list(zip(ytrue, ypred, ious, confs, ytrue_ids, ypred_ids))

        return cls(
            matches,
            eval_key=eval_key,
            gt_field=gt_field,
            pred_field=pred_field,
            classes=classes,
            missing=missing,
            samples=samples,
            **kwargs,
        )


def _parse_config(pred_field, gt_field, method, **kwargs):
    if method is None:
        method = "coco"

    if method == "coco":
        from .coco import COCOEvaluationConfig

        return COCOEvaluationConfig(pred_field, gt_field, **kwargs)

    if method == "open-images":
        from .openimages import OpenImagesEvaluationConfig

        return OpenImagesEvaluationConfig(pred_field, gt_field, **kwargs)

    raise ValueError("Unsupported evaluation method '%s'" % method)


def _tally_matches(matches):
    tp = 0
    fp = 0
    fn = 0
    for match in matches:
        gt_label = match[0]
        pred_label = match[1]
        if gt_label is None:
            fp += 1
        elif pred_label is None:
            fn += 1
        elif gt_label != pred_label:
            fp += 1
            fn += 1
        else:
            tp += 1

    return tp, fp, fn
