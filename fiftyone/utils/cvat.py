"""
Utilities for working with datasets in
`CVAT format <https://github.com/opencv/cvat>`_.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from collections import defaultdict
from copy import copy, deepcopy
from datetime import datetime
import itertools
import logging
import os
import warnings
import webbrowser

from bson import ObjectId
import jinja2
import numpy as np
import requests
import urllib3

import eta.core.data as etad
import eta.core.image as etai
import eta.core.utils as etau

import fiftyone.constants as foc
import fiftyone.core.fields as fof
import fiftyone.core.labels as fol
import fiftyone.core.media as fom
import fiftyone.core.metadata as fomt
import fiftyone.core.utils as fou
import fiftyone.utils.annotations as foua
import fiftyone.utils.data as foud


logger = logging.getLogger(__name__)


class CVATImageDatasetImporter(
    foud.LabeledImageDatasetImporter, foud.ImportPathsMixin
):
    """Importer for CVAT image datasets stored on disk.

    See :ref:`this page <CVATImageDataset-import>` for format details.

    Args:
        dataset_dir (None): the dataset directory. If omitted, ``data_path``
            and/or ``labels_path`` must be provided
        data_path (None): an optional parameter that enables explicit control
            over the location of the media. Can be any of the following:

            -   a folder name like ``"data"`` or ``"data/"`` specifying a
                subfolder of ``dataset_dir`` where the media files reside
            -   an absolute directory path where the media files reside. In
                this case, the ``dataset_dir`` has no effect on the location of
                the data
            -   a filename like ``"data.json"`` specifying the filename of the
                JSON data manifest file in ``dataset_dir``
            -   an absolute filepath specifying the location of the JSON data
                manifest. In this case, ``dataset_dir`` has no effect on the
                location of the data

            If None, this parameter will default to whichever of ``data/`` or
            ``data.json`` exists in the dataset directory
        labels_path (None): an optional parameter that enables explicit control
            over the location of the labels. Can be any of the following:

            -   a filename like ``"labels.xml"`` specifying the location of the
                labels in ``dataset_dir``
            -   an absolute filepath to the labels. In this case,
                ``dataset_dir`` has no effect on the location of the labels

            If None, the parameter will default to ``labels.xml``
        include_all_data (False): whether to generate samples for all images in
            the data directory (True) rather than only creating samples for
            images with label entries (False)
        shuffle (False): whether to randomly shuffle the order in which the
            samples are imported
        seed (None): a random seed to use when shuffling
        max_samples (None): a maximum number of samples to import. By default,
            all samples are imported
    """

    def __init__(
        self,
        dataset_dir=None,
        data_path=None,
        labels_path=None,
        include_all_data=False,
        shuffle=False,
        seed=None,
        max_samples=None,
    ):
        if dataset_dir is None and data_path is None and labels_path is None:
            raise ValueError(
                "At least one of `dataset_dir`, `data_path`, and "
                "`labels_path` must be provided"
            )

        data_path = self._parse_data_path(
            dataset_dir=dataset_dir, data_path=data_path, default="data/",
        )

        labels_path = self._parse_labels_path(
            dataset_dir=dataset_dir,
            labels_path=labels_path,
            default="labels.xml",
        )

        super().__init__(
            dataset_dir=dataset_dir,
            shuffle=shuffle,
            seed=seed,
            max_samples=max_samples,
        )

        self.data_path = data_path
        self.labels_path = labels_path
        self.include_all_data = include_all_data

        self._info = None
        self._image_paths_map = None
        self._cvat_images_map = None
        self._filenames = None
        self._iter_filenames = None
        self._num_samples = None

    def __iter__(self):
        self._iter_filenames = iter(self._filenames)
        return self

    def __len__(self):
        return self._num_samples

    def __next__(self):
        filename = next(self._iter_filenames)

        if os.path.isabs(filename):
            image_path = filename
        else:
            image_path = self._image_paths_map[filename]

        cvat_image = self._cvat_images_map.get(filename, None)
        if cvat_image is not None:
            # Labeled image
            image_metadata = cvat_image.get_image_metadata()
            labels = cvat_image.to_labels()
        else:
            # Unlabeled image
            image_metadata = fomt.ImageMetadata.build_for(image_path)
            labels = None

        return image_path, image_metadata, labels

    @property
    def has_dataset_info(self):
        return True

    @property
    def has_image_metadata(self):
        return True

    @property
    def label_cls(self):
        return {
            "detections": fol.Detections,
            "polylines": fol.Polylines,
            "keypoints": fol.Keypoints,
        }

    def setup(self):
        self._image_paths_map = self._load_data_map(
            self.data_path, recursive=True
        )

        if self.labels_path is not None and os.path.isfile(self.labels_path):
            info, _, cvat_images = load_cvat_image_annotations(
                self.labels_path
            )
        else:
            info = {}
            cvat_images = []

        self._info = info

        # Use subset/name as the key if it exists, else just name
        cvat_images_map = {}
        for i in cvat_images:
            if i.subset:
                key = os.path.join(i.subset, i.name)
            else:
                key = i.name

            cvat_images_map[key] = i

        self._cvat_images_map = cvat_images_map

        filenames = set(self._cvat_images_map.keys())

        if self.include_all_data:
            filenames.update(self._image_paths_map.keys())

        self._filenames = self._preprocess_list(sorted(filenames))
        self._num_samples = len(self._filenames)

    def get_dataset_info(self):
        return self._info


class CVATVideoDatasetImporter(
    foud.LabeledVideoDatasetImporter, foud.ImportPathsMixin
):
    """Importer for CVAT video datasets stored on disk.

    See :ref:`this page <CVATVideoDataset-import>` for format details.

    Args:
        dataset_dir (None): the dataset directory. If omitted, ``data_path``
            and/or ``labels_path`` must be provided
        data_path (None): an optional parameter that enables explicit control
            over the location of the media. Can be any of the following:

            -   a folder name like ``"data"`` or ``"data/"`` specifying a
                subfolder of ``dataset_dir`` where the media files reside
            -   an absolute directory path where the media files reside. In
                this case, the ``dataset_dir`` has no effect on the location of
                the data
            -   a filename like ``"data.json"`` specifying the filename of the
                JSON data manifest file in ``dataset_dir``
            -   an absolute filepath specifying the location of the JSON data
                manifest. In this case, ``dataset_dir`` has no effect on the
                location of the data

            If None, this parameter will default to whichever of ``data/`` or
            ``data.json`` exists in the dataset directory
        labels_path (None): an optional parameter that enables explicit control
            over the location of the labels. Can be any of the following:

            -   a folder name like ``"labels"`` or ``"labels/"`` specifying the
                location of the labels in ``dataset_dir``
            -   an absolute folder path to the labels. In this case,
                ``dataset_dir`` has no effect on the location of the labels

            If None, the parameter will default to ``labels/``
        include_all_data (False): whether to generate samples for all videos in
            the data directory (True) rather than only creating samples for
            videos with label entries (False)
        shuffle (False): whether to randomly shuffle the order in which the
            samples are imported
        seed (None): a random seed to use when shuffling
        max_samples (None): a maximum number of samples to import. By default,
            all samples are imported
    """

    def __init__(
        self,
        dataset_dir=None,
        data_path=None,
        labels_path=None,
        include_all_data=False,
        shuffle=False,
        seed=None,
        max_samples=None,
    ):
        if dataset_dir is None and data_path is None and labels_path is None:
            raise ValueError(
                "At least one of `dataset_dir`, `data_path`, and "
                "`labels_path` must be provided"
            )

        data_path = self._parse_data_path(
            dataset_dir=dataset_dir, data_path=data_path, default="data/",
        )

        labels_path = self._parse_labels_path(
            dataset_dir=dataset_dir,
            labels_path=labels_path,
            default="labels/",
        )

        super().__init__(
            dataset_dir=dataset_dir,
            shuffle=shuffle,
            seed=seed,
            max_samples=max_samples,
        )

        self.data_path = data_path
        self.labels_path = labels_path
        self.include_all_data = include_all_data

        self._info = None
        self._cvat_task_labels = None
        self._video_paths_map = None
        self._labels_paths_map = None
        self._uuids = None
        self._iter_uuids = None
        self._num_samples = None

    def __iter__(self):
        self._iter_uuids = iter(self._uuids)
        return self

    def __len__(self):
        return self._num_samples

    def __next__(self):
        uuid = next(self._iter_uuids)

        video_path = self._video_paths_map[uuid]

        labels_path = self._labels_paths_map.get(uuid, None)
        if labels_path:
            # Labeled video
            info, cvat_task_labels, cvat_tracks = load_cvat_video_annotations(
                labels_path
            )

            if self._info is None:
                self._info = info

            self._cvat_task_labels.merge_task_labels(cvat_task_labels)
            self._info["task_labels"] = self._cvat_task_labels.labels

            frames = _cvat_tracks_to_frames_dict(cvat_tracks)
        else:
            # Unlabeled video
            frames = None

        return video_path, None, None, frames

    @property
    def has_dataset_info(self):
        return True

    @property
    def has_video_metadata(self):
        return False  # has (width, height) but not other important info

    @property
    def label_cls(self):
        return None

    @property
    def frame_labels_cls(self):
        return {
            "detections": fol.Detections,
            "polylines": fol.Polylines,
            "keypoints": fol.Keypoints,
        }

    def setup(self):
        self._video_paths_map = self._load_data_map(
            self.data_path, ignore_exts=True, recursive=True
        )

        if self.labels_path is not None and os.path.isdir(self.labels_path):
            self._labels_paths_map = {
                os.path.splitext(p)[0]: os.path.join(self.labels_path, p)
                for p in etau.list_files(self.labels_path, recursive=True)
            }
        else:
            self._labels_paths_map = {}

        uuids = set(self._labels_paths_map.keys())

        if self.include_all_data:
            uuids.update(self._video_paths_map.keys())

        self._info = None
        self._uuids = self._preprocess_list(sorted(uuids))
        self._num_samples = len(self._uuids)
        self._cvat_task_labels = CVATTaskLabels()

    def get_dataset_info(self):
        return self._info


class CVATImageDatasetExporter(
    foud.LabeledImageDatasetExporter, foud.ExportPathsMixin
):
    """Exporter that writes CVAT image datasets to disk.

    See :ref:`this page <CVATImageDataset-export>` for format details.

    Args:
        export_dir (None): the directory to write the export. This has no
            effect if ``data_path`` and ``labels_path`` are absolute paths
        data_path (None): an optional parameter that enables explicit control
            over the location of the exported media. Can be any of the
            following:

            -   a folder name like ``"data"`` or ``"data/"`` specifying a
                subfolder of ``export_dir`` in which to export the media
            -   an absolute directory path in which to export the media. In
                this case, the ``export_dir`` has no effect on the location of
                the data
            -   a JSON filename like ``"data.json"`` specifying the filename of
                the manifest file in ``export_dir`` generated when
                ``export_media`` is ``"manifest"``
            -   an absolute filepath specifying the location to write the JSON
                manifest file when ``export_media`` is ``"manifest"``. In this
                case, ``export_dir`` has no effect on the location of the data

            If None, the default value of this parameter will be chosen based
            on the value of the ``export_media`` parameter
        labels_path (None): an optional parameter that enables explicit control
            over the location of the exported labels. Can be any of the
            following:

            -   a filename like ``"labels.xml"`` specifying the location in
                ``export_dir`` in which to export the labels
            -   an absolute filepath to which to export the labels. In this
                case, the ``export_dir`` has no effect on the location of the
                labels

            If None, the labels will be exported into ``export_dir`` using the
            default filename
        export_media (None): controls how to export the raw media. The
            supported values are:

            -   ``True``: copy all media files into the output directory
            -   ``False``: don't export media
            -   ``"move"``: move all media files into the output directory
            -   ``"symlink"``: create symlinks to the media files in the output
                directory
            -   ``"manifest"``: create a ``data.json`` in the output directory
                that maps UUIDs used in the labels files to the filepaths of
                the source media, rather than exporting the actual media

            If None, the default value of this parameter will be chosen based
            on the value of the ``data_path`` parameter
        image_format (None): the image format to use when writing in-memory
            images to disk. By default, ``fiftyone.config.default_image_ext``
            is used
    """

    def __init__(
        self,
        export_dir=None,
        data_path=None,
        labels_path=None,
        export_media=None,
        image_format=None,
    ):
        data_path, export_media = self._parse_data_path(
            export_dir=export_dir,
            data_path=data_path,
            export_media=export_media,
            default="data/",
        )

        labels_path = self._parse_labels_path(
            export_dir=export_dir,
            labels_path=labels_path,
            default="labels.xml",
        )

        super().__init__(export_dir=export_dir)

        self.data_path = data_path
        self.labels_path = labels_path
        self.export_media = export_media
        self.image_format = image_format

        self._name = None
        self._task_labels = None
        self._cvat_images = None
        self._media_exporter = None

    @property
    def requires_image_metadata(self):
        return True

    @property
    def label_cls(self):
        return {
            "detections": fol.Detections,
            "polylines": fol.Polylines,
            "keypoints": fol.Keypoints,
        }

    def setup(self):
        self._cvat_images = []
        self._media_exporter = foud.ImageExporter(
            self.export_media,
            export_path=self.data_path,
            default_ext=self.image_format,
        )
        self._media_exporter.setup()

    def log_collection(self, sample_collection):
        self._name = sample_collection.name
        self._task_labels = sample_collection.info.get("task_labels", None)

    def export_sample(self, image_or_path, labels, metadata=None):
        _, uuid = self._media_exporter.export(image_or_path)

        if labels is None:
            return  # unlabeled

        if not isinstance(labels, dict):
            labels = {"labels": labels}

        if all(v is None for v in labels.values()):
            return  # unlabeled

        if metadata is None:
            metadata = fomt.ImageMetadata.build_for(image_or_path)

        cvat_image = CVATImage.from_labels(labels, metadata)

        cvat_image.id = len(self._cvat_images)
        cvat_image.name = uuid

        self._cvat_images.append(cvat_image)

    def close(self, *args):
        # Get task labels
        if self._task_labels is None:
            # Compute task labels from active label schema
            cvat_task_labels = CVATTaskLabels.from_cvat_images(
                self._cvat_images
            )
        else:
            # Use task labels from logged collection info
            cvat_task_labels = CVATTaskLabels(labels=self._task_labels)

        # Write annotations
        writer = CVATImageAnnotationWriter()
        writer.write(
            cvat_task_labels,
            self._cvat_images,
            self.labels_path,
            id=0,
            name=self._name,
        )

        self._media_exporter.close()


class CVATVideoDatasetExporter(
    foud.LabeledVideoDatasetExporter, foud.ExportPathsMixin
):
    """Exporter that writes CVAT video datasets to disk.

    See :ref:`this page <CVATVideoDataset-export>` for format details.

    Args:
        export_dir (None): the directory to write the export. This has no
            effect if ``data_path`` and ``labels_path`` are absolute paths
        data_path (None): an optional parameter that enables explicit control
            over the location of the exported media. Can be any of the
            following:

            -   a folder name like ``"data"`` or ``"data/"`` specifying a
                subfolder of ``export_dir`` in which to export the media
            -   an absolute directory path in which to export the media. In
                this case, the ``export_dir`` has no effect on the location of
                the data
            -   a JSON filename like ``"data.json"`` specifying the filename of
                the manifest file in ``export_dir`` generated when
                ``export_media`` is ``"manifest"``
            -   an absolute filepath specifying the location to write the JSON
                manifest file when ``export_media`` is ``"manifest"``. In this
                case, ``export_dir`` has no effect on the location of the data

            If None, the default value of this parameter will be chosen based
            on the value of the ``export_media`` parameter
        labels_path (None): an optional parameter that enables explicit control
            over the location of the exported labels. Can be any of the
            following:

            -   a folder name like ``"labels"`` or ``"labels/"`` specifying the
                location in ``export_dir`` in which to export the labels
            -   an absolute filepath to which to export the labels. In this
                case, the ``export_dir`` has no effect on the location of the
                labels

            If None, the labels will be exported into ``export_dir`` using the
            default folder name
        export_media (None): controls how to export the raw media. The
            supported values are:

            -   ``True``: copy all media files into the output directory
            -   ``False``: don't export media
            -   ``"move"``: move all media files into the output directory
            -   ``"symlink"``: create symlinks to the media files in the output
                directory
            -   ``"manifest"``: create a ``data.json`` in the output directory
                that maps UUIDs used in the labels files to the filepaths of
                the source media, rather than exporting the actual media

            If None, the default value of this parameter will be chosen based
            on the value of the ``data_path`` parameter
    """

    def __init__(
        self,
        export_dir=None,
        data_path=None,
        labels_path=None,
        export_media=None,
    ):
        data_path, export_media = self._parse_data_path(
            export_dir=export_dir,
            data_path=data_path,
            export_media=export_media,
            default="data/",
        )

        labels_path = self._parse_labels_path(
            export_dir=export_dir, labels_path=labels_path, default="labels/",
        )

        super().__init__(export_dir=export_dir)

        self.data_path = data_path
        self.labels_path = labels_path
        self.export_media = export_media

        self._task_labels = None
        self._num_samples = 0
        self._writer = None
        self._media_exporter = None

    @property
    def requires_video_metadata(self):
        return True

    @property
    def label_cls(self):
        return None

    @property
    def frame_labels_cls(self):
        return {
            "detections": fol.Detections,
            "polylines": fol.Polylines,
            "keypoints": fol.Keypoints,
        }

    def setup(self):
        self._writer = CVATVideoAnnotationWriter()
        self._media_exporter = foud.ImageExporter(
            self.export_media, export_path=self.data_path,
        )
        self._media_exporter.setup()

    def log_collection(self, sample_collection):
        self._task_labels = sample_collection.info.get("task_labels", None)

    def export_sample(self, video_path, _, frames, metadata=None):
        _, filename = self._media_exporter.export(video_path)

        if frames is None:
            return  # unlabeled

        if metadata is None:
            metadata = fomt.VideoMetadata.build_for(video_path)

        out_anno_path = os.path.join(
            self.labels_path, os.path.splitext(filename)[0] + ".xml"
        )

        # Generate object tracks
        frame_size = (metadata.frame_width, metadata.frame_height)
        cvat_tracks = _frames_to_cvat_tracks(frames, frame_size)

        if cvat_tracks is None:
            return  # unlabeled

        # Get task labels
        if self._task_labels is None:
            # Compute task labels from active label schema
            cvat_task_labels = CVATTaskLabels.from_cvat_tracks(cvat_tracks)
        else:
            # Use task labels from logged collection info
            cvat_task_labels = CVATTaskLabels(labels=self._task_labels)

        # Write annotations
        self._num_samples += 1
        self._writer.write(
            cvat_task_labels,
            cvat_tracks,
            metadata,
            out_anno_path,
            id=self._num_samples - 1,
            name=filename,
        )

    def close(self, *args):
        self._media_exporter.close()


class CVATTaskLabels(object):
    """Description of the labels in a CVAT image annotation task.

    Args:
        labels (None): a list of label dicts in the following format::

            [
                {
                    "name": "car",
                    "attributes": [
                        {
                            "name": "type"
                            "categories": ["coupe", "sedan", "truck"]
                        },
                        ...
                    }
                },
                ...
            ]
    """

    def __init__(self, labels=None):
        self.labels = labels or []

    def merge_task_labels(self, task_labels):
        """Merges the given :class:`CVATTaskLabels` into this instance.

        Args:
            task_labels: a :class:`CVATTaskLabels`
        """
        schema = self.to_schema()
        schema.merge_schema(task_labels.to_schema())
        new_task_labels = CVATTaskLabels.from_schema(schema)
        self.labels = new_task_labels.labels

    def to_schema(self):
        """Returns an ``eta.core.image.ImageLabelsSchema`` representation of
        the task labels.

        Note that CVAT's task labels schema does not distinguish between boxes,
        polylines, and keypoints, so the returned schema stores all annotations
        under the ``"objects"`` field.

        Returns:
            an ``eta.core.image.ImageLabelsSchema``
        """
        schema = etai.ImageLabelsSchema()

        for label in self.labels:
            _label = label["name"]
            schema.add_object_label(_label)
            for attribute in label.get("attributes", []):
                _name = attribute["name"]
                _categories = attribute["categories"]
                for _value in _categories:
                    _attr = etad.CategoricalAttribute(_name, _value)
                    schema.add_object_attribute(_label, _attr)

        return schema

    @classmethod
    def from_cvat_images(cls, cvat_images):
        """Creates a :class:`CVATTaskLabels` instance that describes the active
        schema of the given annotations.

        Args:
            cvat_images: a list of :class:`CVATImage` instances

        Returns:
            a :class:`CVATTaskLabels`
        """
        schema = etai.ImageLabelsSchema()
        for cvat_image in cvat_images:
            for anno in cvat_image.iter_annos():
                _label = anno.label
                schema.add_object_label(_label)

                if anno.occluded is not None:
                    _attr = etad.BooleanAttribute("occluded", anno.occluded)
                    schema.add_object_attribute(_label, _attr)

                for attr in anno.attributes:
                    _attr = attr.to_eta_attribute()
                    schema.add_object_attribute(_label, _attr)

        return cls.from_schema(schema)

    @classmethod
    def from_cvat_tracks(cls, cvat_tracks):
        """Creates a :class:`CVATTaskLabels` instance that describes the active
        schema of the given annotations.

        Args:
            cvat_tracks: a list of :class:`CVATTrack` instances

        Returns:
            a :class:`CVATTaskLabels`
        """
        schema = etai.ImageLabelsSchema()
        for cvat_track in cvat_tracks:
            for anno in cvat_track.iter_annos():
                _label = anno.label
                schema.add_object_label(_label)

                if anno.outside is not None:
                    _attr = etad.BooleanAttribute("outside", anno.outside)
                    schema.add_object_attribute(_label, _attr)

                if anno.occluded is not None:
                    _attr = etad.BooleanAttribute("occluded", anno.occluded)
                    schema.add_object_attribute(_label, _attr)

                if anno.keyframe is not None:
                    _attr = etad.BooleanAttribute("keyframe", anno.keyframe)
                    schema.add_object_attribute(_label, _attr)

                for attr in anno.attributes:
                    _attr = attr.to_eta_attribute()
                    schema.add_object_attribute(_label, _attr)

        return cls.from_schema(schema)

    @classmethod
    def from_labels_dict(cls, d):
        """Creates a :class:`CVATTaskLabels` instance from the ``<labels>``
        tag of a CVAT annotation XML file.

        Args:
            d: a dict representation of a ``<labels>`` tag

        Returns:
            a :class:`CVATTaskLabels`
        """
        labels = _ensure_list(d.get("label", []))
        _labels = []
        for label in labels:
            _tmp = label.get("attributes", None) or {}
            attributes = _ensure_list(_tmp.get("attribute", []))
            _attributes = []
            for attribute in attributes:
                _values = attribute.get("values", None)
                _categories = _values.split("\n") if _values else []
                _attributes.append(
                    {"name": attribute["name"], "categories": _categories}
                )

            _labels.append({"name": label["name"], "attributes": _attributes})

        return cls(labels=_labels)

    @classmethod
    def from_schema(cls, schema):
        """Creates a :class:`CVATTaskLabels` instance from an
        ``eta.core.image.ImageLabelsSchema``.

        Args:
            schema: an ``eta.core.image.ImageLabelsSchema``

        Returns:
            a :class:`CVATTaskLabels`
        """
        labels = []
        obj_schemas = schema.objects
        for label in sorted(obj_schemas.schema):
            obj_schema = obj_schemas.schema[label]
            obj_attr_schemas = obj_schema.attrs
            attributes = []
            for name in sorted(obj_attr_schemas.schema):
                attr_schema = obj_attr_schemas.schema[name]
                if isinstance(attr_schema, etad.CategoricalAttributeSchema):
                    attributes.append(
                        {
                            "name": name,
                            "categories": sorted(attr_schema.categories),
                        }
                    )

            labels.append({"name": label, "attributes": attributes})

        return cls(labels=labels)


class CVATImage(object):
    """An annotated image in CVAT image format.

    Args:
        id: the ID of the image
        name: the filename of the image
        width: the width of the image, in pixels
        height: the height of the image, in pixels
        boxes (None): a list of :class:`CVATImageBox` instances
        polygons (None): a list of :class:`CVATImagePolygon` instances
        polylines (None): a list of :class:`CVATImagePolyline` instances
        points (None): a list of :class:`CVATImagePoints` instances
        subset (None): the project subset of the image, if any
    """

    def __init__(
        self,
        id,
        name,
        width,
        height,
        boxes=None,
        polygons=None,
        polylines=None,
        points=None,
        subset=None,
    ):
        self.id = id
        self.name = name
        self.subset = subset
        self.width = width
        self.height = height
        self.boxes = boxes or []
        self.polygons = polygons or []
        self.polylines = polylines or []
        self.points = points or []

    @property
    def has_boxes(self):
        """Whether this image has 2D boxes."""
        return bool(self.boxes)

    @property
    def has_polylines(self):
        """Whether this image has polygons or polylines."""
        return bool(self.polygons) or bool(self.polylines)

    @property
    def has_points(self):
        """Whether this image has keypoints."""
        return bool(self.points)

    def iter_annos(self):
        """Returns an iterator over the annotations in the image.

        Returns:
            an iterator that emits :class:`CVATImageAnno` instances
        """
        return itertools.chain(
            self.boxes, self.polygons, self.polylines, self.points
        )

    def get_image_metadata(self):
        """Returns a :class:`fiftyone.core.metadata.ImageMetadata` instance for
        the annotations.

        Returns:
            a :class:`fiftyone.core.metadata.ImageMetadata`
        """
        return fomt.ImageMetadata(width=self.width, height=self.height)

    def to_labels(self):
        """Returns :class:`fiftyone.core.labels.Label` representations of the
        annotations.

        Returns:
            a dict mapping field keys to :class:`fiftyone.core.labels.Label`
            instances
        """
        frame_size = (self.width, self.height)

        labels = {}

        if self.boxes:
            detections = [b.to_detection(frame_size) for b in self.boxes]
            labels["detections"] = fol.Detections(detections=detections)

        if self.polygons or self.polylines:
            polygons = [p.to_polyline(frame_size) for p in self.polygons]
            polylines = [p.to_polyline(frame_size) for p in self.polylines]
            labels["polylines"] = fol.Polylines(polylines=polygons + polylines)

        if self.points:
            keypoints = [k.to_keypoint(frame_size) for k in self.points]
            labels["keypoints"] = fol.Keypoints(keypoints=keypoints)

        return labels

    @classmethod
    def from_labels(cls, labels, metadata):
        """Creates a :class:`CVATImage` from a dictionary of labels.

        Args:
            labels: a dict mapping keys to :class:`fiftyone.core.labels.Label`
                instances
            metadata: a :class:`fiftyone.core.metadata.ImageMetadata` for the
                image

        Returns:
            a :class:`CVATImage`
        """
        width = metadata.width
        height = metadata.height

        _detections = []
        _polygons = []
        _polylines = []
        _keypoints = []
        for _labels in labels.values():
            if isinstance(_labels, fol.Detection):
                _detections.append(_labels)
            elif isinstance(_labels, fol.Detections):
                _detections.extend(_labels.detections)
            elif isinstance(_labels, fol.Polyline):
                if _labels.closed:
                    _polygons.append(_labels)
                else:
                    _polylines.append(_labels)
            elif isinstance(_labels, fol.Polylines):
                for poly in _labels.polylines:
                    if poly.closed:
                        _polygons.append(poly)
                    else:
                        _polylines.append(poly)
            elif isinstance(_labels, fol.Keypoint):
                _keypoints.append(_labels)
            elif isinstance(_labels, fol.Keypoints):
                _keypoints.extend(_labels.keypoints)
            elif _labels is not None:
                msg = (
                    "Ignoring unsupported label type '%s'" % _labels.__class__
                )
                warnings.warn(msg)

        boxes = [CVATImageBox.from_detection(d, metadata) for d in _detections]

        polygons = []
        for p in _polygons:
            polygons.extend(CVATImagePolygon.from_polyline(p, metadata))

        polylines = []
        for p in _polylines:
            polylines.extend(CVATImagePolyline.from_polyline(p, metadata))

        points = [
            CVATImagePoints.from_keypoint(k, metadata) for k in _keypoints
        ]

        return cls(
            None,
            None,
            width,
            height,
            boxes=boxes,
            polygons=polygons,
            polylines=polylines,
            points=points,
        )

    @classmethod
    def from_image_dict(cls, d):
        """Creates a :class:`CVATImage` from an ``<image>`` tag of a CVAT image
        annotations XML file.

        Args:
            d: a dict representation of an ``<image>`` tag

        Returns:
            a :class:`CVATImage`
        """
        id = d["@id"]
        name = d["@name"]
        subset = d.get("@subset", None)
        width = int(d["@width"])
        height = int(d["@height"])

        boxes = []
        for bd in _ensure_list(d.get("box", [])):
            boxes.append(CVATImageBox.from_box_dict(bd))

        polygons = []
        for pd in _ensure_list(d.get("polygon", [])):
            polygons.append(CVATImagePolygon.from_polygon_dict(pd))

        polylines = []
        for pd in _ensure_list(d.get("polyline", [])):
            polylines.append(CVATImagePolyline.from_polyline_dict(pd))

        points = []
        for pd in _ensure_list(d.get("points", [])):
            points.append(CVATImagePoints.from_points_dict(pd))

        return cls(
            id,
            name,
            width,
            height,
            boxes=boxes,
            polygons=polygons,
            polylines=polylines,
            points=points,
            subset=subset,
        )


class HasCVATPoints(object):
    """Mixin for CVAT annotations that store a list of ``(x, y)`` pixel
    coordinates.

    Attributes:
        points: a list of ``(x, y)`` pixel coordinates defining points
    """

    def __init__(self, points):
        self.points = points

    @property
    def points_str(self):
        return self._to_cvat_points_str(self.points)

    @staticmethod
    def _to_rel_points(points, frame_size):
        w, h = frame_size
        return [(x / w, y / h) for x, y in points]

    @staticmethod
    def _to_abs_points(points, frame_size):
        w, h = frame_size
        return [(int(round(x * w)), int(round(y * h))) for x, y in points]

    @staticmethod
    def _to_cvat_points_str(points):
        return ";".join("%g,%g" % (x, y) for x, y in points)

    @staticmethod
    def _parse_cvat_points_str(points_str):
        points = []
        for xy_str in points_str.split(";"):
            x, y = xy_str.split(",")
            points.append((int(round(float(x))), int(round(float(y)))))

        return points


class CVATImageAnno(object):
    """Mixin for annotations in CVAT image format.

    Args:
        occluded (None): whether the object is occluded
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(self, occluded=None, attributes=None):
        self.occluded = occluded
        self.attributes = attributes or []

    def _to_attributes(self):
        attributes = {a.name: a.value for a in self.attributes}

        if self.occluded == 1:
            attributes["occluded"] = True

        return attributes

    @staticmethod
    def _parse_attributes(label):
        attrs = dict(label.iter_attributes())

        occluded = _to_int_bool(attrs.pop("occluded", None))

        attributes = [
            CVATAttribute(k, v)
            for k, v in attrs.items()
            if _is_supported_attribute_type(v)
        ]

        return occluded, attributes

    @staticmethod
    def _parse_anno_dict(d):
        occluded = _from_int_bool(d.get("@occluded", None))

        attributes = []
        for attr in _ensure_list(d.get("attribute", [])):
            if "#text" in attr:
                name = attr["@name"].lstrip("@")
                if name == "label_id":
                    # We assume that this is a `label_id` exported from an
                    # CVAT annotation run created by our annotation API, which
                    # should be ignored since we're not using the API here
                    continue

                value = _parse_value(attr["#text"])
                attributes.append(CVATAttribute(name, value))

        return occluded, attributes


class CVATImageBox(CVATImageAnno):
    """An object bounding box in CVAT image format.

    Args:
        label: the object label string
        xtl: the top-left x-coordinate of the box, in pixels
        ytl: the top-left y-coordinate of the box, in pixels
        xbr: the bottom-right x-coordinate of the box, in pixels
        ybr: the bottom-right y-coordinate of the box, in pixels
        occluded (None): whether the object is occluded
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(
        self, label, xtl, ytl, xbr, ybr, occluded=None, attributes=None
    ):
        self.label = label
        self.xtl = xtl
        self.ytl = ytl
        self.xbr = xbr
        self.ybr = ybr
        CVATImageAnno.__init__(self, occluded=occluded, attributes=attributes)

    def to_detection(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Detection` representation of
        the box.

        Args:
            frame_size: the ``(width, height)`` of the image

        Returns:
            a :class:`fiftyone.core.labels.Detection`
        """
        label = self.label

        width, height = frame_size
        bounding_box = [
            self.xtl / width,
            self.ytl / height,
            (self.xbr - self.xtl) / width,
            (self.ybr - self.ytl) / height,
        ]

        attributes = self._to_attributes()

        return fol.Detection(
            label=label, bounding_box=bounding_box, **attributes
        )

    @classmethod
    def from_detection(cls, detection, metadata):
        """Creates a :class:`CVATImageBox` from a
        :class:`fiftyone.core.labels.Detection`.

        Args:
            detection: a :class:`fiftyone.core.labels.Detection`
            metadata: a :class:`fiftyone.core.metadata.ImageMetadata` for the
                image

        Returns:
            a :class:`CVATImageBox`
        """
        label = detection.label

        width = metadata.width
        height = metadata.height
        x, y, w, h = detection.bounding_box
        xtl = int(round(x * width))
        ytl = int(round(y * height))
        xbr = int(round((x + w) * width))
        ybr = int(round((y + h) * height))

        occluded, attributes = cls._parse_attributes(detection)

        return cls(
            label, xtl, ytl, xbr, ybr, occluded=occluded, attributes=attributes
        )

    @classmethod
    def from_box_dict(cls, d):
        """Creates a :class:`CVATImageBox` from a ``<box>`` tag of a CVAT image
        annotation XML file.

        Args:
            d: a dict representation of a ``<box>`` tag

        Returns:
            a :class:`CVATImageBox`
        """
        label = d["@label"]

        xtl = int(round(float(d["@xtl"])))
        ytl = int(round(float(d["@ytl"])))
        xbr = int(round(float(d["@xbr"])))
        ybr = int(round(float(d["@ybr"])))

        occluded, attributes = cls._parse_anno_dict(d)

        return cls(
            label, xtl, ytl, xbr, ybr, occluded=occluded, attributes=attributes
        )


class CVATImagePolygon(CVATImageAnno, HasCVATPoints):
    """A polygon in CVAT image format.

    Args:
        label: the polygon label string
        points: a list of ``(x, y)`` pixel coordinates defining the vertices of
            the polygon
        occluded (None): whether the polygon is occluded
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(self, label, points, occluded=None, attributes=None):
        self.label = label
        HasCVATPoints.__init__(self, points)
        CVATImageAnno.__init__(self, occluded=occluded, attributes=attributes)

    def to_polyline(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Polyline` representation of
        the polygon.

        Args:
            frame_size: the ``(width, height)`` of the image

        Returns:
            a :class:`fiftyone.core.labels.Polyline`
        """
        label = self.label
        points = self._to_rel_points(self.points, frame_size)
        attributes = self._to_attributes()
        return fol.Polyline(
            label=label,
            points=[points],
            closed=True,
            filled=True,
            **attributes,
        )

    @classmethod
    def from_polyline(cls, polyline, metadata):
        """Creates a :class:`CVATImagePolygon` from a
        :class:`fiftyone.core.labels.Polyline`.

        If the :class:`fiftyone.core.labels.Polyline` is composed of multiple
        shapes, one :class:`CVATImagePolygon` per shape will be generated.

        Args:
            polyline: a :class:`fiftyone.core.labels.Polyline`
            metadata: a :class:`fiftyone.core.metadata.ImageMetadata` for the
                image

        Returns:
            a list of :class:`CVATImagePolygon` instances
        """
        label = polyline.label

        if len(polyline.points) > 1:
            msg = (
                "Found polyline with more than one shape; generating separate "
                "annotations for each shape"
            )
            warnings.warn(msg)

        frame_size = (metadata.width, metadata.height)
        occluded, attributes = cls._parse_attributes(polyline)

        polylines = []
        for points in polyline.points:
            abs_points = cls._to_abs_points(points, frame_size)
            polylines.append(
                cls(
                    label, abs_points, occluded=occluded, attributes=attributes
                )
            )

        return polylines

    @classmethod
    def from_polygon_dict(cls, d):
        """Creates a :class:`CVATImagePolygon` from a ``<polygon>`` tag of a
        CVAT image annotation XML file.

        Args:
            d: a dict representation of a ``<polygon>`` tag

        Returns:
            a :class:`CVATImagePolygon`
        """
        label = d["@label"]
        points = cls._parse_cvat_points_str(d["@points"])
        occluded, attributes = cls._parse_anno_dict(d)

        return cls(label, points, occluded=occluded, attributes=attributes)


class CVATImagePolyline(CVATImageAnno, HasCVATPoints):
    """A polyline in CVAT image format.

    Args:
        label: the polyline label string
        points: a list of ``(x, y)`` pixel coordinates defining the vertices of
            the polyline
        occluded (None): whether the polyline is occluded
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(self, label, points, occluded=None, attributes=None):
        self.label = label
        HasCVATPoints.__init__(self, points)
        CVATImageAnno.__init__(self, occluded=occluded, attributes=attributes)

    def to_polyline(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Polyline` representation of
        the polyline.

        Args:
            frame_size: the ``(width, height)`` of the image

        Returns:
            a :class:`fiftyone.core.labels.Polyline`
        """
        label = self.label
        points = self._to_rel_points(self.points, frame_size)
        attributes = self._to_attributes()
        return fol.Polyline(
            label=label,
            points=[points],
            closed=False,
            filled=False,
            **attributes,
        )

    @classmethod
    def from_polyline(cls, polyline, metadata):
        """Creates a :class:`CVATImagePolyline` from a
        :class:`fiftyone.core.labels.Polyline`.

        If the :class:`fiftyone.core.labels.Polyline` is composed of multiple
        shapes, one :class:`CVATImagePolyline` per shape will be generated.

        Args:
            polyline: a :class:`fiftyone.core.labels.Polyline`
            metadata: a :class:`fiftyone.core.metadata.ImageMetadata` for the
                image

        Returns:
            a list of :class:`CVATImagePolyline` instances
        """
        label = polyline.label

        if len(polyline.points) > 1:
            msg = (
                "Found polyline with more than one shape; generating separate "
                "annotations for each shape"
            )
            warnings.warn(msg)

        frame_size = (metadata.width, metadata.height)
        occluded, attributes = cls._parse_attributes(polyline)

        polylines = []
        for points in polyline.points:
            abs_points = cls._to_abs_points(points, frame_size)
            if abs_points and polyline.closed:
                abs_points.append(copy(abs_points[0]))

            polylines.append(
                cls(
                    label, abs_points, occluded=occluded, attributes=attributes
                )
            )

        return polylines

    @classmethod
    def from_polyline_dict(cls, d):
        """Creates a :class:`CVATImagePolyline` from a ``<polyline>`` tag of a
        CVAT image annotation XML file.

        Args:
            d: a dict representation of a ``<polyline>`` tag

        Returns:
            a :class:`CVATImagePolyline`
        """
        label = d["@label"]
        points = cls._parse_cvat_points_str(d["@points"])
        occluded, attributes = cls._parse_anno_dict(d)

        return cls(label, points, occluded=occluded, attributes=attributes)


class CVATImagePoints(CVATImageAnno, HasCVATPoints):
    """A set of keypoints in CVAT image format.

    Args:
        label: the keypoints label string
        points: a list of ``(x, y)`` pixel coordinates defining the vertices of
            the keypoints
        occluded (None): whether the keypoints are occluded
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(self, label, points, occluded=None, attributes=None):
        self.label = label
        HasCVATPoints.__init__(self, points)
        CVATImageAnno.__init__(self, occluded=occluded, attributes=attributes)

    def to_keypoint(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Keypoint` representation of
        the points.

        Args:
            frame_size: the ``(width, height)`` of the image

        Returns:
            a :class:`fiftyone.core.labels.Keypoint`
        """
        label = self.label
        points = self._to_rel_points(self.points, frame_size)
        attributes = self._to_attributes()
        return fol.Keypoint(label=label, points=points, **attributes)

    @classmethod
    def from_keypoint(cls, keypoint, metadata):
        """Creates a :class:`CVATImagePoints` from a
        :class:`fiftyone.core.labels.Keypoint`.

        Args:
            keypoint: a :class:`fiftyone.core.labels.Keypoint`
            metadata: a :class:`fiftyone.core.metadata.ImageMetadata` for the
                image

        Returns:
            a :class:`CVATImagePoints`
        """
        label = keypoint.label

        frame_size = (metadata.width, metadata.height)
        points = cls._to_abs_points(keypoint.points, frame_size)

        occluded, attributes = cls._parse_attributes(keypoint)

        return cls(label, points, occluded=occluded, attributes=attributes)

    @classmethod
    def from_points_dict(cls, d):
        """Creates a :class:`CVATImagePoints` from a ``<points>`` tag of a
        CVAT image annotation XML file.

        Args:
            d: a dict representation of a ``<points>`` tag

        Returns:
            a :class:`CVATImagePoints`
        """
        label = d["@label"]
        points = cls._parse_cvat_points_str(d["@points"])
        occluded, attributes = cls._parse_anno_dict(d)
        return cls(label, points, occluded=occluded, attributes=attributes)


class CVATTrack(object):
    """An annotation track in CVAT video format.

    Args:
        id: the ID of the track
        label: the label for the track
        width: the width of the video frames, in pixels
        height: the height of the video frames, in pixels
        boxes (None): a dict mapping frame numbers to :class:`CVATVideoBox`
            instances
        polygons (None): a dict mapping frame numbers to
            :class:`CVATVideoPolygon` instances
        polylines (None): a dict mapping frame numbers to
            :class:`CVATVideoPolyline` instances
        points (None): a dict mapping frame numbers to :class:`CVATVideoPoints`
            instances
    """

    def __init__(
        self,
        id,
        label,
        width,
        height,
        boxes=None,
        polygons=None,
        polylines=None,
        points=None,
    ):
        self.id = id
        self.label = label
        self.width = width
        self.height = height
        self.boxes = boxes or {}
        self.polygons = polygons or {}
        self.polylines = polylines or {}
        self.points = points or {}

    @property
    def has_boxes(self):
        """Whether this track has 2D boxes."""
        return bool(self.boxes)

    @property
    def has_polylines(self):
        """Whether this track has polygons or polylines."""
        return bool(self.polygons) or bool(self.polylines)

    @property
    def has_points(self):
        """Whether this track has keypoints."""
        return bool(self.points)

    def iter_annos(self):
        """Returns an iterator over the annotations in the track.

        Returns:
            an iterator that emits :class:`CVATVideoAnno` instances
        """
        return itertools.chain(
            self.boxes.values(),
            self.polygons.values(),
            self.polylines.values(),
            self.points.values(),
        )

    def to_labels(self):
        """Returns :class:`fiftyone.core.labels.Label` representations of the
        annotations.

        Returns:
            a dict mapping frame numbers to
            :class:`fiftyone.core.labels.Label` instances
        """
        frame_size = (self.width, self.height)

        labels = {}

        # Only one of these will actually contain labels

        for frame_number, box in self.boxes.items():
            if box.outside != 1:
                detection = box.to_detection(frame_size)
                detection.index = self.id
                labels[frame_number + 1] = detection

        for frame_number, polygon in self.polygons.items():
            if polygon.outside != 1:
                polyline = polygon.to_polyline(frame_size)
                polyline.index = self.id
                labels[frame_number + 1] = polyline

        for frame_number, polyline in self.polylines.items():
            if polyline.outside != 1:
                polyline = polyline.to_polyline(frame_size)
                polyline.index = self.id
                labels[frame_number + 1] = polyline

        for frame_number, points in self.points.items():
            if points.outside != 1:
                keypoint = points.to_keypoint(frame_size)
                keypoint.index = self.id
                labels[frame_number + 1] = keypoint

        return labels

    @classmethod
    def from_labels(cls, id, labels, frame_size):
        """Creates a :class:`CVATTrack` from a dictionary of labels.

        Args:
            id: the ID of the track
            labels: a dict mapping frame numbers to
                :class:`fiftyone.core.labels.Label` instances
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`CVATTrack`
        """
        width, height = frame_size

        boxes = {}
        polygons = {}
        polylines = {}
        points = {}
        label = None
        for fn, _label in labels.items():
            label = _label.label

            if isinstance(_label, fol.Detection):
                boxes[fn - 1] = CVATVideoBox.from_detection(
                    fn, _label, frame_size
                )
            elif isinstance(_label, fol.Polyline):
                if _label.filled:
                    polygons[fn - 1] = CVATVideoPolygon.from_polyline(
                        fn, _label, frame_size
                    )
                else:
                    polylines[fn - 1] = CVATVideoPolyline.from_polyline(
                        fn, _label, frame_size
                    )
            elif isinstance(_label, fol.Keypoint):
                points[fn - 1] = CVATVideoPoints.from_keypoint(
                    fn, _label, frame_size
                )
            elif _label is not None:
                msg = "Ignoring unsupported label type '%s'" % _label.__class__
                warnings.warn(msg)

        # CVAT uses `outside=1` to mark the end of track segments, while
        # FiftyOne implicitly represents this by missing labels. So, we need to
        # convert to CVAT format here
        cls._add_outside_shapes(boxes)
        cls._add_outside_shapes(polygons)
        cls._add_outside_shapes(polylines)
        cls._add_outside_shapes(points)

        return cls(
            id,
            label,
            width,
            height,
            boxes=boxes,
            polygons=polygons,
            polylines=polylines,
            points=points,
        )

    @classmethod
    def from_track_dict(cls, d, frame_size):
        """Creates a :class:`CVATTrack` from a ``<track>`` tag of a CVAT video
        annotation XML file.

        Args:
            d: a dict representation of an ``<track>`` tag
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`CVATTrack`
        """
        id = d["@id"]
        label = d["@label"]

        width, height = frame_size

        boxes = {}
        for bd in _ensure_list(d.get("box", [])):
            box = CVATVideoBox.from_box_dict(label, bd)
            boxes[box.frame] = box

        polygons = {}
        for pd in _ensure_list(d.get("polygon", [])):
            polygon = CVATVideoPolygon.from_polygon_dict(label, pd)
            polygons[polygon.frame] = polygon

        polylines = {}
        for pd in _ensure_list(d.get("polyline", [])):
            polyline = CVATVideoPolyline.from_polyline_dict(label, pd)
            polylines[polyline.frame] = polyline

        points = {}
        for pd in _ensure_list(d.get("points", [])):
            point = CVATVideoPoints.from_points_dict(label, pd)
            points[point.frame] = point

        return cls(
            id,
            label,
            width,
            height,
            boxes=boxes,
            polygons=polygons,
            polylines=polylines,
            points=points,
        )

    @staticmethod
    def _add_outside_shapes(shapes):
        if not shapes:
            return

        use_keyframes = any(s.keyframe for s in shapes.values())

        def _make_outside_shape(shape):
            shape = deepcopy(shape)
            shape.outside = 1
            if use_keyframes:
                shape.keyframe = 1

            return shape

        # Add "outside" shapes to represent gaps of >= 1 frame in tracks
        fns = sorted(shapes.keys())
        last_fn = fns[0]
        for fn in fns:
            if fn > last_fn + 1:
                shapes[last_fn + 1] = _make_outside_shape(shapes[last_fn])

            last_fn = fn

        # Always add an "outside" shape to the end of each track
        shapes[last_fn + 1] = _make_outside_shape(shapes[last_fn])


class CVATVideoAnno(object):
    """Mixin for annotations in CVAT video format.

    Args:
        outside (None): whether the object is outside (invisible)
        occluded (None): whether the object is occluded
        keyframe (None): whether the frame is a keyframe
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(
        self, outside=None, occluded=None, keyframe=None, attributes=None
    ):
        self.outside = outside
        self.occluded = occluded
        self.keyframe = keyframe
        self.attributes = attributes or []

    def _to_attributes(self):
        attributes = {a.name: a.value for a in self.attributes}

        # We don't include `outside` here because shapes marked as `outside`
        # are completely omitted

        if self.occluded == 1:
            attributes["occluded"] = True

        if self.keyframe == 1:
            attributes["keyframe"] = True

        return attributes

    @staticmethod
    def _parse_attributes(label):
        attrs = dict(label.iter_attributes())

        outside = 0  # any FiftyOne label is implicitly not `outside`
        occluded = _to_int_bool(attrs.pop("occluded", None))
        keyframe = _to_int_bool(attrs.pop("keyframe", None))

        attributes = [
            CVATAttribute(k, v)
            for k, v in attrs.items()
            if _is_supported_attribute_type(v)
        ]

        return outside, occluded, keyframe, attributes

    @staticmethod
    def _parse_anno_dict(d):
        outside = _from_int_bool(d.get("@outside", None))
        occluded = _from_int_bool(d.get("@occluded", None))
        keyframe = _from_int_bool(d.get("@keyframe", None))

        attributes = []
        for attr in _ensure_list(d.get("attribute", [])):
            if "#text" in attr:
                name = attr["@name"].lstrip("@")
                if name == "label_id":
                    # We assume that this is a `label_id` exported from an
                    # CVAT annotation run created by our annotation API, which
                    # should be ignored since we're not using the API here
                    continue

                value = _parse_value(attr["#text"])
                attributes.append(CVATAttribute(name, value))

        return outside, occluded, keyframe, attributes


class CVATVideoBox(CVATVideoAnno):
    """An object bounding box in CVAT video format.

    Args:
        frame: the 0-based frame number
        label: the object label string
        xtl: the top-left x-coordinate of the box, in pixels
        ytl: the top-left y-coordinate of the box, in pixels
        xbr: the bottom-right x-coordinate of the box, in pixels
        ybr: the bottom-right y-coordinate of the box, in pixels
        outside (None): whether the object is outside (invisible)
        occluded (None): whether the object is occluded
        keyframe (None): whether the frame is a keyframe
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(
        self,
        frame,
        label,
        xtl,
        ytl,
        xbr,
        ybr,
        outside=None,
        occluded=None,
        keyframe=None,
        attributes=None,
    ):
        self.frame = frame
        self.label = label
        self.xtl = xtl
        self.ytl = ytl
        self.xbr = xbr
        self.ybr = ybr
        CVATVideoAnno.__init__(
            self,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    def to_detection(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Detection` representation of
        the box.

        Args:
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`fiftyone.core.labels.Detection`
        """
        label = self.label

        width, height = frame_size
        bounding_box = [
            self.xtl / width,
            self.ytl / height,
            (self.xbr - self.xtl) / width,
            (self.ybr - self.ytl) / height,
        ]

        attributes = self._to_attributes()

        return fol.Detection(
            label=label, bounding_box=bounding_box, **attributes
        )

    @classmethod
    def from_detection(cls, frame_number, detection, frame_size):
        """Creates a :class:`CVATVideoBox` from a
        :class:`fiftyone.core.labels.Detection`.

        Args:
            frame_number: the frame number
            detection: a :class:`fiftyone.core.labels.Detection`
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`CVATVideoBox`
        """
        frame = frame_number - 1
        label = detection.label

        width, height = frame_size
        x, y, w, h = detection.bounding_box
        xtl = int(round(x * width))
        ytl = int(round(y * height))
        xbr = int(round((x + w) * width))
        ybr = int(round((y + h) * height))

        outside, occluded, keyframe, attributes = cls._parse_attributes(
            detection
        )

        return cls(
            frame,
            label,
            xtl,
            ytl,
            xbr,
            ybr,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    @classmethod
    def from_box_dict(cls, label, d):
        """Creates a :class:`CVATVideoBox` from a ``<box>`` tag of a CVAT video
        annotation XML file.

        Args:
            label: the object label
            d: a dict representation of a ``<box>`` tag

        Returns:
            a :class:`CVATVideoBox`
        """
        frame = int(d["@frame"])

        xtl = int(round(float(d["@xtl"])))
        ytl = int(round(float(d["@ytl"])))
        xbr = int(round(float(d["@xbr"])))
        ybr = int(round(float(d["@ybr"])))

        outside, occluded, keyframe, attributes = cls._parse_anno_dict(d)

        return cls(
            frame,
            label,
            xtl,
            ytl,
            xbr,
            ybr,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )


class CVATVideoPolygon(CVATVideoAnno, HasCVATPoints):
    """A polygon in CVAT video format.

    Args:
        frame: the 0-based frame number
        label: the polygon label string
        points: a list of ``(x, y)`` pixel coordinates defining the vertices of
            the polygon
        outside (None): whether the polygon is outside (invisible)
        occluded (None): whether the polygon is occluded
        keyframe (None): whether the frame is a keyframe
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(
        self,
        frame,
        label,
        points,
        outside=None,
        occluded=None,
        keyframe=None,
        attributes=None,
    ):
        self.frame = frame
        self.label = label
        HasCVATPoints.__init__(self, points)
        CVATVideoAnno.__init__(
            self,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    def to_polyline(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Polyline` representation of
        the polygon.

        Args:
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`fiftyone.core.labels.Polyline`
        """
        label = self.label
        points = self._to_rel_points(self.points, frame_size)
        attributes = self._to_attributes()
        return fol.Polyline(
            label=label,
            points=[points],
            closed=True,
            filled=True,
            **attributes,
        )

    @classmethod
    def from_polyline(cls, frame_number, polyline, frame_size):
        """Creates a :class:`CVATVideoPolygon` from a
        :class:`fiftyone.core.labels.Polyline`.

        Args:
            frame_number: the frame number
            polyline: a :class:`fiftyone.core.labels.Polyline`
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`CVATVideoPolygon`
        """
        frame = frame_number - 1
        label = polyline.label

        points = _get_single_polyline_points(polyline)
        points = cls._to_abs_points(points, frame_size)

        outside, occluded, keyframe, attributes = cls._parse_attributes(
            polyline
        )

        return cls(
            frame,
            label,
            points,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    @classmethod
    def from_polygon_dict(cls, label, d):
        """Creates a :class:`CVATVideoPolygon` from a ``<polygon>`` tag of a
        CVAT video annotation XML file.

        Args:
            label: the object label
            d: a dict representation of a ``<polygon>`` tag

        Returns:
            a :class:`CVATVideoPolygon`
        """
        frame = int(d["@frame"])
        points = cls._parse_cvat_points_str(d["@points"])
        outside, occluded, keyframe, attributes = cls._parse_anno_dict(d)
        return cls(
            frame,
            label,
            points,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )


class CVATVideoPolyline(CVATVideoAnno, HasCVATPoints):
    """A polyline in CVAT video format.

    Args:
        frame: the 0-based frame number
        label: the polyline label string
        points: a list of ``(x, y)`` pixel coordinates defining the vertices of
            the polyline
        outside (None): whether the polyline is outside (invisible)
        occluded (None): whether the polyline is occluded
        keyframe (None): whether the frame is a keyframe
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(
        self,
        frame,
        label,
        points,
        outside=None,
        occluded=None,
        keyframe=None,
        attributes=None,
    ):
        self.frame = frame
        self.label = label
        HasCVATPoints.__init__(self, points)
        CVATVideoAnno.__init__(
            self,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    def to_polyline(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Polyline` representation of
        the polyline.

        Args:
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`fiftyone.core.labels.Polyline`
        """
        label = self.label
        points = self._to_rel_points(self.points, frame_size)
        attributes = self._to_attributes()
        return fol.Polyline(
            label=label,
            points=[points],
            closed=False,
            filled=False,
            **attributes,
        )

    @classmethod
    def from_polyline(cls, frame_number, polyline, frame_size):
        """Creates a :class:`CVATVideoPolyline` from a
        :class:`fiftyone.core.labels.Polyline`.

        Args:
            frame_number: the frame number
            polyline: a :class:`fiftyone.core.labels.Polyline`
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`CVATVideoPolyline`
        """
        frame = frame_number - 1
        label = polyline.label

        points = _get_single_polyline_points(polyline)
        points = cls._to_abs_points(points, frame_size)
        if points and polyline.closed:
            points.append(copy(points[0]))

        outside, occluded, keyframe, attributes = cls._parse_attributes(
            polyline
        )

        return cls(
            frame,
            label,
            points,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    @classmethod
    def from_polyline_dict(cls, label, d):
        """Creates a :class:`CVATVideoPolyline` from a ``<polyline>`` tag of a
        CVAT video annotation XML file.

        Args:
            label: the object label
            d: a dict representation of a ``<polyline>`` tag

        Returns:
            a :class:`CVATVideoPolyline`
        """
        frame = int(d["@frame"])
        points = cls._parse_cvat_points_str(d["@points"])
        outside, occluded, keyframe, attributes = cls._parse_anno_dict(d)
        return cls(
            frame,
            label,
            points,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )


class CVATVideoPoints(CVATVideoAnno, HasCVATPoints):
    """A set of keypoints in CVAT video format.

    Args:
        frame: the 0-based frame number
        label: the keypoints label string
        points: a list of ``(x, y)`` pixel coordinates defining the keypoints
        outside (None): whether the keypoints is outside (invisible)
        occluded (None): whether the keypoints are occluded
        keyframe (None): whether the frame is a keyframe
        attributes (None): a list of :class:`CVATAttribute` instances
    """

    def __init__(
        self,
        frame,
        label,
        points,
        outside=None,
        occluded=None,
        keyframe=None,
        attributes=None,
    ):
        self.frame = frame
        self.label = label
        HasCVATPoints.__init__(self, points)
        CVATVideoAnno.__init__(
            self,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    def to_keypoint(self, frame_size):
        """Returns a :class:`fiftyone.core.labels.Keypoint` representation of
        the points.

        Args:
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`fiftyone.core.labels.Keypoint`
        """
        label = self.label
        points = self._to_rel_points(self.points, frame_size)
        attributes = self._to_attributes()
        return fol.Keypoint(label=label, points=points, **attributes)

    @classmethod
    def from_keypoint(cls, frame_number, keypoint, frame_size):
        """Creates a :class:`CVATVideoPoints` from a
        :class:`fiftyone.core.labels.Keypoint`.

        Args:
            frame_number: the frame number
            keypoint: a :class:`fiftyone.core.labels.Keypoint`
            frame_size: the ``(width, height)`` of the video frames

        Returns:
            a :class:`CVATVideoPoints`
        """
        frame = frame_number - 1
        label = keypoint.label
        points = cls._to_abs_points(keypoint.points, frame_size)
        outside, occluded, keyframe, attributes = cls._parse_attributes(
            keypoint
        )
        return cls(
            frame,
            label,
            points,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )

    @classmethod
    def from_points_dict(cls, label, d):
        """Creates a :class:`CVATVideoPoints` from a ``<points>`` tag of a
        CVAT video annotation XML file.

        Args:
            label: the object label
            d: a dict representation of a ``<points>`` tag

        Returns:
            a :class:`CVATVideoPoints`
        """
        frame = int(d["@frame"])
        points = cls._parse_cvat_points_str(d["@points"])
        outside, occluded, keyframe, attributes = cls._parse_anno_dict(d)
        return cls(
            frame,
            label,
            points,
            outside=outside,
            occluded=occluded,
            keyframe=keyframe,
            attributes=attributes,
        )


class CVATAttribute(object):
    """An attribute in CVAT image format.

    Args:
        name: the attribute name
        value: the attribute value
    """

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def to_eta_attribute(self):
        """Returns an ``eta.core.data.Attribute`` representation of the
        attribute.

        Returns:
            an ``eta.core.data.Attribute``
        """
        if isinstance(self.value, bool):
            return etad.BooleanAttribute(self.name, self.value)

        if etau.is_numeric(self.value):
            return etad.NumericAttribute(self.name, self.value)

        return etad.CategoricalAttribute(self.name, self.value)

    def to_attribute(self):
        """Returns a :class:`fiftyone.core.labels.Attribute` representation of
        the attribute.
        Returns:
            a :class:`fiftyone.core.labels.Attribute`
        """
        if isinstance(self.value, bool):
            return fol.BooleanAttribute(value=self.value)

        if etau.is_numeric(self.value):
            return fol.NumericAttribute(value=self.value)

        return fol.CategoricalAttribute(value=self.value)


class CVATImageAnnotationWriter(object):
    """Class for writing annotations in CVAT image format.

    See :ref:`this page <CVATImageDataset-export>` for format details.
    """

    def __init__(self):
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(foc.RESOURCES_DIR),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.template = environment.get_template(
            "cvat_image_annotation_template.xml"
        )

    def write(
        self, cvat_task_labels, cvat_images, xml_path, id=None, name=None
    ):
        """Writes the annotations to disk.

        Args:
            cvat_task_labels: a :class:`CVATTaskLabels` instance
            cvat_images: a list of :class:`CVATImage` instances
            xml_path: the path to write the annotations XML file
            id (None): an ID for the task
            name (None): a name for the task
        """
        now = datetime.now().isoformat()
        xml_str = self.template.render(
            {
                "id": id,
                "name": name,
                "size": len(cvat_images),
                "created": now,
                "updated": now,
                "labels": cvat_task_labels.labels,
                "dumped": now,
                "images": cvat_images,
            }
        )
        etau.write_file(xml_str, xml_path)


class CVATVideoAnnotationWriter(object):
    """Class for writing annotations in CVAT video format.

    See :ref:`this page <CVATVideoDataset-export>` for format details.
    """

    def __init__(self):
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(foc.RESOURCES_DIR),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.template = environment.get_template(
            "cvat_video_interpolation_template.xml"
        )

    def write(
        self,
        cvat_task_labels,
        cvat_tracks,
        metadata,
        xml_path,
        id=None,
        name=None,
    ):
        """Writes the annotations to disk.

        Args:
            cvat_task_labels: a :class:`CVATTaskLabels` instance
            cvat_tracks: a list of :class:`CVATTrack` instances
            metadata: the :class:`fiftyone.core.metadata.VideoMetadata`
                instance for the video
            xml_path: the path to write the annotations XML file
            id (None): an ID for the task
            name (None): a name for the task
        """
        now = datetime.now().isoformat()
        xml_str = self.template.render(
            {
                "id": id,
                "name": name,
                "size": metadata.total_frame_count,
                "created": now,
                "updated": now,
                "width": metadata.frame_width,
                "height": metadata.frame_height,
                "labels": cvat_task_labels.labels,
                "dumped": now,
                "tracks": cvat_tracks,
            }
        )
        etau.write_file(xml_str, xml_path)


class CVATBackendConfig(foua.AnnotationBackendConfig):
    """Base class for configuring :class:`CVATBackend` instances.

    Args:
        name: the name of the backend
        label_schema: a dictionary containing the description of label fields,
            classes and attribute to annotate
        media_field ("filepath"): string field name containing the paths to
            media files on disk to upload
        url (None): the url of the CVAT server
        username (None): the CVAT username
        password (None): the CVAT password
        segment_size (None): maximum number of images per job. Not applicable
            to videos
        image_quality (75): an int in `[0, 100]` determining the image quality
            to upload to CVAT
        use_cache (True): whether to use a cache when uploading data. Using a
            cache reduces task creation time as data will be processed
            on-the-fly and stored in the cache when requested
        use_zip_chunks (True): when annotating videos, whether to upload video
            frames in smaller chunks. Setting this option to ``False`` may
            result in reduced video quality in CVAT due to size limitations on
            ZIP files that can be uploaded to CVAT
        chunk_size (None): the number of frames to upload per ZIP chunk
        task_assignee (None): the username(s) to which the task(s) were
            assigned. This argument can be a list of usernames when annotating
            videos as each video is uploaded to a separate task
        job_assignees (None): a list of usernames to which jobs were assigned
        job_reviewers (None): a list of usernames to which job reviews were
            assigned
        project_name (None): an optional project name to which to upload the
            created CVAT task. If a project with this name is found, it will be
            used, otherwise a new project with this name is created. By
            default, no project is used
        project_id (None): an optional ID of an existing CVAT project to which
            to upload the annotation tasks. By default, no project is used
        occluded_attr (None): an optional attribute name containing existing
            occluded values and/or in which to store downloaded occluded values
            for all objects in the annotation run
    """

    def __init__(
        self,
        name,
        label_schema,
        media_field="filepath",
        url=None,
        username=None,
        password=None,
        segment_size=None,
        image_quality=75,
        use_cache=True,
        use_zip_chunks=True,
        chunk_size=None,
        task_assignee=None,
        job_assignees=None,
        job_reviewers=None,
        project_name=None,
        project_id=None,
        occluded_attr=None,
        **kwargs,
    ):
        super().__init__(name, label_schema, media_field=media_field, **kwargs)
        self.url = url
        self.segment_size = segment_size
        self.image_quality = image_quality
        self.use_cache = use_cache
        self.use_zip_chunks = use_zip_chunks
        self.chunk_size = chunk_size
        self.task_assignee = task_assignee
        self.job_assignees = job_assignees
        self.job_reviewers = job_reviewers
        self.project_name = project_name
        self.project_id = project_id
        self.occluded_attr = occluded_attr

        # store privately so these aren't serialized
        self._username = username
        self._password = password

    @property
    def username(self):
        return self._username

    @username.setter
    def username(self, value):
        self._username = value

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        self._password = value


class CVATBackend(foua.AnnotationBackend):
    """Class for interacting with the CVAT annotation backend."""

    @property
    def supported_label_types(self):
        return [
            "classification",
            "classifications",
            "detection",
            "detections",
            "instance",
            "instances",
            "polyline",
            "polylines",
            "polygon",
            "polygons",
            "keypoint",
            "keypoints",
            "segmentation",
            "scalar",
        ]

    @property
    def supported_scalar_types(self):
        return [
            fof.IntField,
            fof.FloatField,
            fof.StringField,
            fof.BooleanField,
        ]

    @property
    def supported_attr_types(self):
        return [
            "text",
            "select",
            "radio",
            "checkbox",
            "occluded",
        ]

    @property
    def supports_keyframes(self):
        return True

    @property
    def requires_label_schema(self):
        return False  # schemas can be inferred from existing CVAT projects

    def recommend_attr_tool(self, name, value):
        if isinstance(value, bool):
            if name == "occluded":
                return {"type": "occluded"}

            return {"type": "checkbox"}

        return {"type": "text"}

    def requires_attr_values(self, attr_type):
        return attr_type in ("select", "radio")

    def connect_to_api(self):
        return CVATAnnotationAPI(
            self.config.name,
            self.config.url,
            username=self.config.username,
            password=self.config.password,
        )

    def upload_annotations(self, samples, launch_editor=False):
        api = self.connect_to_api()

        logger.info("Uploading samples to CVAT...")
        results = api.upload_samples(samples, self)
        logger.info("Upload complete")

        if launch_editor:
            results.launch_editor()

        return results

    def download_annotations(self, results):
        api = self.connect_to_api()

        logger.info("Downloading labels from CVAT...")
        annotations = api.download_annotations(results)
        logger.info("Download complete")

        return annotations


class CVATAnnotationResults(foua.AnnotationResults):
    """Class that stores all relevant information needed to monitor the
    progress of an annotation run sent to CVAT and download the results.
    """

    def __init__(
        self,
        samples,
        config,
        id_map,
        server_id_map,
        project_ids,
        task_ids,
        job_ids,
        frame_id_map,
        labels_task_map,
        backend=None,
    ):
        super().__init__(samples, config, id_map, backend=backend)

        self.server_id_map = server_id_map
        self.project_ids = project_ids
        self.task_ids = task_ids
        self.job_ids = job_ids
        self.frame_id_map = frame_id_map
        self.labels_task_map = labels_task_map

    def load_credentials(self, url=None, username=None, password=None):
        """Load the CVAT credentials from the given keyword arguments or the
        FiftyOne annotation config.

        Args:
            url (None): the url of the CVAT server
            username (None): the CVAT username
            password (None): the CVAT password
        """
        self._load_config_parameters(
            url=url, username=username, password=password
        )

    def connect_to_api(self):
        """Returns an API instance connected to the CVAT server.

        Returns:
            a :class:`CVATAnnotationAPI`
        """
        return self._backend.connect_to_api()

    def launch_editor(self):
        """Launches the CVAT editor and loads the first task for this
        annotation run.
        """
        api = self.connect_to_api()
        task_id = self.task_ids[0]
        job_ids = self.job_ids

        if job_ids and job_ids[task_id]:
            editor_url = api.base_job_url(task_id, job_ids[task_id][0])
        else:
            editor_url = api.base_task_url(task_id)

        logger.info("Launching editor at '%s'...", editor_url)
        api.launch_editor(url=editor_url)

    def get_status(self):
        """Gets the status of the assigned tasks and jobs.

        Returns:
            a dict of status information
        """
        return self._get_status()

    def print_status(self):
        """Print the status of the assigned tasks and jobs."""
        self._get_status(log=True)

    def cleanup(self):
        """Deletes all tasks associated with this annotation run and any created
        projects from the CVAT server.
        """
        api = self.connect_to_api()

        if self.task_ids:
            logger.info("Deleting tasks...")
            api.delete_tasks(self.task_ids)

        if self.project_ids:
            projects_to_delete = api.get_empty_projects(self.project_ids)
            if projects_to_delete:
                logger.info("Deleting projects...")
                api.delete_projects(self.project_ids)

        # @todo save updated results to DB?
        self.project_ids = []
        self.task_ids = []
        self.job_ids = {}

    def _get_status(self, log=False):
        api = self.connect_to_api()

        status = {}
        for label_field, task_ids in self.labels_task_map.items():
            if log:
                logger.info("\nStatus for label field '%s':\n", label_field)

            status[label_field] = {}

            for task_id in task_ids:
                task_url = api.task_url(task_id)

                try:
                    task_json = api.get(task_url).json()
                except:
                    logger.warning(
                        "\tFailed to get info for task '%d' at %s",
                        task_id,
                        task_url,
                    )
                    continue

                task_name = task_json["name"]
                task_status = task_json["status"]
                task_assignee = task_json["assignee"]
                task_updated = task_json["updated_date"]

                if log:
                    logger.info(
                        "\tTask %d (%s):\n"
                        "\t\tStatus: %s\n"
                        "\t\tAssignee: %s\n"
                        "\t\tLast updated: %s\n"
                        "\t\tURL: %s\n",
                        task_id,
                        task_name,
                        task_status,
                        task_assignee,
                        task_updated,
                        api.base_task_url(task_id),
                    )

                jobs_info = {}
                for job_id in self.job_ids[task_id]:
                    job_url = api.taskless_job_url(job_id)

                    try:
                        job_json = api.get(job_url).json()
                    except:
                        logger.warning(
                            "\t\tFailed to get info for job '%d' at %s",
                            job_id,
                            job_url,
                        )
                        continue

                    jobs_info[job_id] = job_json

                    if log:
                        logger.info(
                            "\t\tJob %d:\n"
                            "\t\t\tStatus: %s\n"
                            "\t\t\tAssignee: %s\n"
                            "\t\t\tReviewer: %s\n",
                            job_id,
                            job_json["status"],
                            job_json["assignee"],
                            job_json["reviewer"],
                        )

                status[label_field][task_id] = {
                    "name": task_name,
                    "status": task_status,
                    "assignee": task_assignee,
                    "last_updated": task_updated,
                    "jobs": jobs_info,
                }

        return status

    @classmethod
    def _from_dict(cls, d, samples, config):
        # int keys were serialized as strings...
        job_ids = {int(task_id): ids for task_id, ids in d["job_ids"].items()}
        frame_id_map = {
            int(task_id): {
                int(frame_id): frame_data
                for frame_id, frame_data in frame_map.items()
            }
            for task_id, frame_map in d["frame_id_map"].items()
        }

        return cls(
            samples,
            config,
            d["id_map"],
            d.get("server_id_map", {}),
            d.get("project_ids", []),
            d["task_ids"],
            job_ids,
            frame_id_map,
            d["labels_task_map"],
        )


class CVATAnnotationAPI(foua.AnnotationAPI):
    """A class to facilitate connection to and management of tasks in CVAT.

    On initializiation, this class constructs a session based on the provided
    server url and credentials.

    This API provides methods to easily get, put, post, patch, and delete tasks
    and jobs through the formatted urls specified by the CVAT REST API.

    Additionally, samples and label schemas can be uploaded and annotations
    downloaded through this class.

    Args:
        name: the name of the backend
        url: url of the CVAT server
        username (None): the CVAT username
        password (None): the CVAT password
    """

    def __init__(self, name, url, username=None, password=None):
        self._name = name
        self._url = url
        self._username = username
        self._password = password

        self._session = None
        self._user_id_map = {}
        self._project_id_map = {}

        self._setup()

    @property
    def base_url(self):
        return self._url

    @property
    def base_api_url(self):
        return "%s/api/v1" % self.base_url

    @property
    def login_url(self):
        return "%s/auth/login" % self.base_api_url

    @property
    def users_url(self):
        return "%s/users" % self.base_api_url

    @property
    def projects_url(self):
        return "%s/projects" % self.base_api_url

    def project_url(self, project_id):
        return "%s/%d" % (self.projects_url, project_id)

    @property
    def tasks_url(self):
        return "%s/tasks" % self.base_api_url

    def task_url(self, task_id):
        return "%s/%d" % (self.tasks_url, task_id)

    def task_data_url(self, task_id):
        return "%s/data" % self.task_url(task_id)

    def task_data_meta_url(self, task_id):
        return "%s/data/meta" % self.task_url(task_id)

    def task_annotation_url(self, task_id):
        return "%s/annotations" % self.task_url(task_id)

    def task_annotation_formatted_url(
        self, task_id, anno_filepath, anno_format="CVAT 1.1",
    ):
        return "%s/annotations?format=%s&filename=%s" % (
            self.task_url(task_id),
            anno_format,
            anno_filepath,
        )

    def jobs_url(self, task_id):
        return "%s/jobs" % self.task_url(task_id)

    def job_url(self, task_id, job_id):
        return "%s/%d" % (self.jobs_url(task_id), job_id)

    def taskless_job_url(self, job_id):
        return "%s/jobs/%d" % (self.base_api_url, job_id)

    def base_task_url(self, task_id):
        return "%s/tasks/%d" % (self.base_url, task_id)

    def base_job_url(self, task_id, job_id):
        return "%s/tasks/%d/jobs/%d" % (self.base_url, task_id, job_id)

    def user_search_url(self, username):
        return "%s/users?search=%s" % (self.base_api_url, username)

    def project_search_url(self, project_name):
        return "%s/projects?search=%s" % (self.base_api_url, project_name)

    def project_id_search_url(self, project_id):
        return "%s/projects?id=%d" % (self.base_api_url, project_id)

    def _setup(self):
        if not self._url:
            raise ValueError(
                "You must provide/configure the `url` of the CVAT server"
            )

        username = self._username
        password = self._password

        if username is None or password is None:
            username, password = self._prompt_username_password(
                self._name, username=username, password=password
            )

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._session = requests.Session()
        response = self.post(
            self.login_url, data={"username": username, "password": password}
        )

        if "csrftoken" in response.cookies:
            self._session.headers["X-CSRFToken"] = response.cookies[
                "csrftoken"
            ]

    def get(self, url, **kwargs):
        """Sends a GET request to the given CVAT API URL.

        Args:
            url: the url
            **kwargs: additional request parameters

        Returns:
            the request response
        """
        response = self._session.get(url, verify=False, **kwargs)
        self._validate(response, kwargs)
        return response

    def patch(self, url, **kwargs):
        """Sends a PATCH request to the given CVAT API URL.

        Args:
            url: the url
            **kwargs: additional request parameters

        Returns:
            the request response
        """
        response = self._session.patch(url, verify=False, **kwargs)
        self._validate(response, kwargs)
        return response

    def post(self, url, **kwargs):
        """Sends a POST request to the given CVAT API URL.

        Args:
            url: the url
            **kwargs: additional request parameters

        Returns:
            the request response
        """
        response = self._session.post(url, verify=False, **kwargs)
        self._validate(response, kwargs)
        return response

    def put(self, url, **kwargs):
        """Sends a PUT request to the given CVAT API URL.

        Args:
            url: the url
            **kwargs: additional request parameters

        Returns:
            the request response
        """
        response = self._session.put(url, verify=False, **kwargs)
        self._validate(response, kwargs)
        return response

    def delete(self, url, **kwargs):
        """Sends a DELETE request to the given CVAT API URL.

        Args:
            url: the url to send the request to
            **kwargs: additional request parameters

        Returns:
            the request response
        """
        response = self._session.delete(url, verify=False, **kwargs)
        self._validate(response, kwargs)
        return response

    def _get_value_from_search(
        self, search_url_fcn, target, target_key, value_key
    ):
        search_url = search_url_fcn(target)
        resp = self.get(search_url).json()
        for info in resp["results"]:
            if info[target_key] == target:
                return info[value_key]

        return None

    def _get_value_update_map(
        self, name, id_map, result_name, search_url_fcn, name_type
    ):
        if name is None:
            return None

        if name in id_map:
            return id_map[name]

        _id = self._get_value_from_search(
            search_url_fcn, name, result_name, "id"
        )

        if _id is not None:
            id_map[name] = _id

        return _id

    def get_user_id(self, username):
        """Retrieves the CVAT user ID for the given username.

        Args:
            username: the username

        Returns:
            the user ID, or None if the user was not found
        """
        user_id = self._get_value_update_map(
            username,
            self._user_id_map,
            "username",
            self.user_search_url,
            "User",
        )

        if username is not None and user_id is None:
            logger.warning("User '%s' not found", username)

        return user_id

    def get_project_id(self, project_name):
        """Retrieves the CVAT project ID for the first instance of the given
        project name.

        Args:
            project_name: the name of the project

        Returns:
            the project ID, or None if no project with the given name was found
        """
        return self._get_value_update_map(
            project_name,
            self._project_id_map,
            "name",
            self.project_search_url,
            "Project",
        )

    def get_project_name(self, project_id):
        """Retrieves the CVAT project name for the given project ID.

        Args:
            project_id: the ID of the project

        Returns:
            the project name, or None if no project with the given ID was found
        """
        id_map = {i: n for n, i in self._project_id_map.items()}
        project_name = id_map.get(project_id)
        if project_name:
            return project_name

        return self._get_value_from_search(
            self.project_id_search_url, project_id, "id", "name",
        )

    def get_empty_projects(self, project_ids):
        """Check all given project ids to determine if they are empty or if
        they contain at least one task.

        Args:
            project_ids: a list of project ids to check

        Returns:
            a list of empty project ids
        """
        return [pid for pid in project_ids if self._is_empty_project(pid)]

    def _is_empty_project(self, project_id):
        resp = self.get(self.project_url(project_id)).json()
        return not resp["tasks"]

    def create_project(self, name, schema=None):
        """Creates a project on the CVAT server using the given label schema.

        Args:
            name: a name for the project
            schema (None): the label schema to use for the created project

        Returns:
            the ID of the created project in CVAT
        """
        if schema is None:
            schema = {}

        labels = [
            {"name": name, "attributes": list(attributes.values())}
            for name, attributes in schema.items()
        ]

        project_json = {
            "name": name,
            "labels": labels,
        }

        project_resp = self.post(self.projects_url, json=project_json).json()
        return project_resp["id"]

    def create_task(
        self,
        name,
        schema=None,
        segment_size=None,
        image_quality=75,
        task_assignee=None,
        project_id=None,
    ):
        """Creates a task on the CVAT server using the given label schema.

        Args:
            name: a name for the task
            schema (None): the label schema to use for the created task
            segment_size (None): maximum number of images to load into a job.
                Not applicable to videos
            image_quality (75): an int in `[0, 100]` determining the image
                quality to upload to CVAT
            task_assignee (None): the username to assign the created task(s)
            project_id (None): the ID of a project to which upload the task

        Returns:
            a tuple of

            -   **task_id**: the ID of the created task in CVAT
            -   **class_id_map**: a dictionary mapping the IDs assigned to
                classes by CVAT
            -   **attr_id_map**: a dictionary mapping the IDs assigned to
                attributes by CVAT for every class
        """
        task_json = {
            "name": name,
            "image_quality": image_quality,
        }

        if project_id is not None:
            task_json.update({"labels": [], "project_id": project_id})
        else:
            if schema is None:
                schema = {}

            labels = [
                {"name": name, "attributes": list(attributes.values())}
                for name, attributes in schema.items()
            ]

            task_json.update({"labels": labels})

        if segment_size is not None:
            task_json["segment_size"] = segment_size

        task_resp = self.post(self.tasks_url, json=task_json).json()
        task_id = task_resp["id"]

        class_id_map = {}
        attr_id_map = {}
        for label in task_resp["labels"]:
            class_id = label["id"]
            class_id_map[label["name"]] = class_id
            attr_id_map[class_id] = {}
            for attr in label["attributes"]:
                attr_name = attr["name"]
                attr_id = attr["id"]
                attr_id_map[class_id][attr_name] = attr_id

        if task_assignee is not None:
            user_id = self.get_user_id(task_assignee)
            if user_id is not None:
                task_patch = {"assignee_id": self.get_user_id(task_assignee)}
                self.patch(self.task_url(task_id), json=task_patch)

        return task_id, class_id_map, attr_id_map

    def delete_project(self, project_id):
        """Deletes the given project from the CVAT server.

        Args:
            project_id: the project ID
        """
        self.delete(self.project_url(project_id))

    def delete_projects(self, project_ids):
        """Deletes the given projects from the CVAT server.

        Args:
            project_ids: an iterable of project IDs
        """
        with fou.ProgressBar() as pb:
            for project_id in pb(list(project_ids)):
                self.delete_project(project_id)

    def delete_task(self, task_id):
        """Deletes the given task from the CVAT server.

        Args:
            task_id: the task ID
        """
        self.delete(self.task_url(task_id))

    def delete_tasks(self, task_ids):
        """Deletes the given tasks from the CVAT server.

        Args:
            task_ids: an iterable of task IDs
        """
        with fou.ProgressBar() as pb:
            for task_id in pb(list(task_ids)):
                self.delete_task(task_id)

    def launch_editor(self, url=None):
        """Launches the CVAT editor in your default web browser.

        Args:
            url (None): an optional URL to open. By default, the base URL of
                the server is opened
        """
        if url is None:
            url = self.base_url

        webbrowser.open(url, new=2)

    def upload_data(
        self,
        task_id,
        paths,
        image_quality=75,
        use_cache=True,
        use_zip_chunks=True,
        chunk_size=None,
        job_assignees=None,
        job_reviewers=None,
    ):
        """Uploads a list of media to the task with the given ID.

        Args:
            task_id: the task ID
            paths: a list of media paths to upload
            image_quality (75): an int in `[0, 100]` determining the image
                quality to upload to CVAT
            use_cache (True): whether to use a cache when uploading data. Using
                a cache reduces task creation time as data will be processed
                on-the-fly and stored in the cache when requested
            use_zip_chunks (True): when annotating videos, whether to upload
                video frames in smaller chunks. Setting this option to
                ``False`` may result in reduced video quality in CVAT due to
                size limitations on ZIP files that can be uploaded to CVAT
            chunk_size (None): the number of frames to upload per ZIP chunk
            job_assignees (None): a list of usernames to assign jobs
            job_reviewers (None): a list of usernames to assign job reviews

        Returns:
            a list of the job IDs created for the task
        """
        data = {
            "image_quality": image_quality,
            "use_cache": use_cache,
            "use_zip_chunks": use_zip_chunks,
        }

        if chunk_size:
            data["chunk_size"] = chunk_size

        files = {}
        for idx, path in enumerate(paths):
            # IMPORTANT: CVAT organizes media within a task alphabetically by
            # filename, so we must give CVAT filenames whose alphabetical order
            # matches the order of `paths`
            filename = "%06d_%s" % (idx, os.path.basename(path))
            files["client_files[%d]" % idx] = (filename, open(path, "rb"))

        self.post(self.task_data_url(task_id), data=data, files=files)

        # @todo is this loop really needed?
        job_ids = []
        while not job_ids:
            job_resp = self.get(self.jobs_url(task_id))
            job_ids = [j["id"] for j in job_resp.json()]

        if job_assignees is not None:
            num_assignees = len(job_assignees)
            for idx, job_id in enumerate(job_ids):
                # Round robin strategy
                assignee = job_assignees[idx % num_assignees]

                user_id = self.get_user_id(assignee)
                if assignee is not None and user_id is not None:
                    job_patch = {"assignee_id": user_id}
                    self.patch(self.taskless_job_url(job_id), json=job_patch)

        if job_reviewers is not None:
            num_reviewers = len(job_reviewers)
            for idx, job_id in enumerate(job_ids):
                # Round robin strategy
                reviewer = job_reviewers[idx % num_reviewers]

                user_id = self.get_user_id(reviewer)
                if reviewer is not None and user_id is not None:
                    job_patch = {"reviewer_id": user_id}
                    self.patch(self.taskless_job_url(job_id), json=job_patch)

        return job_ids

    def upload_samples(self, samples, backend):
        """Uploads the given samples to CVAT according to the given backend's
        annotation and server configuration.

        Args:
            samples: a :class:`fiftyone.core.collections.SampleCollection` to
                upload to CVAT
            backend: a :class:`CVATBackend` to use to perform the upload

        Returns:
            a :class:`CVATAnnotationResults`
        """
        config = backend.config
        label_schema = config.label_schema
        occluded_attr = config.occluded_attr
        project_name, project_id = self._parse_project_details(
            config.project_name, config.project_id
        )

        # When using an existing project, we cannot support multiple label
        # fields of the same type, since it would not be clear which field
        # labels should be downloaded into
        if project_id is not None:
            self._ensure_one_field_per_type(label_schema)

        id_map = {}
        project_ids = []
        task_ids = []
        job_ids = {}
        frame_id_map = {}
        labels_task_map = {}

        num_samples = len(samples)
        batch_size = self._get_batch_size(samples)

        (
            cvat_schema,
            assign_scalar_attrs,
            occluded_attrs,
            _,
        ) = self._get_cvat_schema(
            label_schema, project_id=project_id, occluded_attr=occluded_attr
        )

        # When adding to an existing project, its label schema is inherited, so
        # we need to store the updated one
        if project_id is not None or occluded_attr is not None:
            config.label_schema = label_schema

        for idx, offset in enumerate(range(0, num_samples, batch_size)):
            samples_batch = samples[offset : (offset + batch_size)]
            anno_tags = []
            anno_shapes = []
            anno_tracks = []

            for label_field, label_info in label_schema.items():
                _tags = []
                _shapes = []
                _tracks = []

                if label_field not in id_map:
                    id_map[label_field] = {}

                if label_field not in labels_task_map:
                    labels_task_map[label_field] = []

                if label_info.get("existing_field", False):
                    label_type = label_info["type"]
                    only_keyframes = label_info.get("only_keyframes", False)

                    self._update_shapes_tags_tracks(
                        _tags,
                        _shapes,
                        _tracks,
                        id_map,
                        label_type,
                        samples_batch,
                        label_field,
                        label_info,
                        cvat_schema,
                        assign_scalar_attrs,
                        only_keyframes,
                        occluded_attrs,
                    )

                anno_tags.extend(_tags)
                anno_shapes.extend(_shapes)
                anno_tracks.extend(_tracks)

            # We must do this here because `cvat_schema` may be altered the
            # first time shapes are created
            if project_id is None and project_name is not None:
                project_id = self.create_project(project_name, cvat_schema)
                project_ids.append(project_id)

            task_name = (
                "FiftyOne_%s"
                % samples_batch._root_dataset.name.replace(" ", "_")
            )
            task_id, class_id_map, attr_id_map = self._create_task_upload_data(
                config,
                idx,
                task_name,
                cvat_schema,
                project_id,
                samples_batch,
                task_ids,
                job_ids,
                frame_id_map,
            )

            for label_field in label_schema.keys():
                labels_task_map[label_field].append(task_id)

            server_id_map = self._upload_annotations(
                anno_shapes,
                anno_tags,
                anno_tracks,
                class_id_map,
                attr_id_map,
                task_id,
            )

        return CVATAnnotationResults(
            samples,
            config,
            id_map,
            server_id_map,
            project_ids,
            task_ids,
            job_ids,
            frame_id_map,
            labels_task_map,
            backend=backend,
        )

    def download_annotations(self, results):
        """Download the annotations from the CVAT server for the given results
        instance and parses them into the appropriate FiftyOne types.

        Args:
            results: a :class:`CVATAnnotationResults`

        Returns:
            the annotations dict
        """
        label_schema = results.config.label_schema
        occluded_attr = results.config.occluded_attr
        id_map = results.id_map
        server_id_map = results.server_id_map
        task_ids = results.task_ids
        frame_id_map = results.frame_id_map
        labels_task_map = results.labels_task_map

        _, project_id = self._parse_project_details(
            results.config.project_name, results.config.project_id
        )

        if results.project_ids:
            # This task created the project, so we know that `label_schema` is
            # already complete and we don't need `project_id` to help us here
            project_id = None

        (
            _,
            assigned_scalar_attrs,
            occluded_attrs,
            label_field_classes,
        ) = self._get_cvat_schema(
            label_schema, project_id=project_id, occluded_attr=occluded_attr
        )

        labels_task_map_rev = defaultdict(list)
        for lf, tasks in labels_task_map.items():
            for task in tasks:
                labels_task_map_rev[task].append(lf)

        annotations = {}

        for task_id in task_ids:
            # Download task data
            task_json = self.get(self.task_url(task_id)).json()
            attr_id_map = {}
            _class_map = {}
            labels = task_json["labels"]
            for label in labels:
                _class_map[label["id"]] = label["name"]
                attr_id_map[label["id"]] = {
                    i["name"]: i["id"] for i in label["attributes"]
                }

            _class_map_rev = {n: i for i, n in _class_map.items()}

            task_resp = self.get(self.task_annotation_url(task_id)).json()
            all_shapes = task_resp["shapes"]
            all_tags = task_resp["tags"]
            all_tracks = task_resp["tracks"]

            data_resp = self.get(self.task_data_meta_url(task_id)).json()
            frames = data_resp["frames"]

            label_fields = labels_task_map_rev[task_id]
            label_types = self._get_return_label_types(
                label_schema, label_fields
            )

            for lf_ind, label_field in enumerate(label_fields):
                label_info = label_schema[label_field]
                label_type = label_info.get("type", None)
                scalar_attrs = assigned_scalar_attrs.get(label_field, False)
                _occluded_attrs = occluded_attrs.get(label_field, {})
                _id_map = id_map.get(label_field, {})

                label_field_results = {}

                # Dict mapping class labels to the classes used in CVAT.
                # These are equal unless a class appears in multiple fields
                _classes = label_field_classes[label_field]

                # Maps CVAT IDs to FiftyOne labels
                class_map = {
                    _class_map_rev[name_lf]: name
                    for name, name_lf in _classes.items()
                }

                _cvat_classes = class_map.keys()
                tags, shapes, tracks = self._filter_field_classes(
                    all_tags, all_shapes, all_tracks, _cvat_classes,
                )

                is_last_field = lf_ind == len(label_fields) - 1
                ignore_types = self._get_ignored_types(
                    project_id, label_types, label_type, is_last_field
                )

                tag_results = self._parse_shapes_tags(
                    "tags",
                    tags,
                    frame_id_map[task_id],
                    label_type,
                    _id_map,
                    server_id_map.get("tags", {}),
                    class_map,
                    attr_id_map,
                    frames,
                    ignore_types,
                    assigned_scalar_attrs=scalar_attrs,
                )
                label_field_results = self._merge_results(
                    label_field_results, tag_results
                )

                shape_results = self._parse_shapes_tags(
                    "shapes",
                    shapes,
                    frame_id_map[task_id],
                    label_type,
                    _id_map,
                    server_id_map.get("shapes", {}),
                    class_map,
                    attr_id_map,
                    frames,
                    ignore_types,
                    assigned_scalar_attrs=scalar_attrs,
                    occluded_attrs=_occluded_attrs,
                )
                label_field_results = self._merge_results(
                    label_field_results, shape_results
                )

                for track_index, track in enumerate(tracks, 1):
                    label_id = track["label_id"]
                    shapes = track["shapes"]
                    for shape in shapes:
                        shape["label_id"] = label_id

                    immutable_attrs = track["attributes"]

                    track_shape_results = self._parse_shapes_tags(
                        "track",
                        shapes,
                        frame_id_map[task_id],
                        label_type,
                        _id_map,
                        server_id_map.get("tracks", {}),
                        class_map,
                        attr_id_map,
                        frames,
                        ignore_types,
                        assigned_scalar_attrs=scalar_attrs,
                        track_index=track_index,
                        immutable_attrs=immutable_attrs,
                        occluded_attrs=_occluded_attrs,
                    )
                    label_field_results = self._merge_results(
                        label_field_results, track_shape_results
                    )

                frames_metadata = {}
                for cvat_frame_id, frame_data in frame_id_map[task_id].items():
                    sample_id = frame_data["sample_id"]
                    if "frame_id" in frame_data and len(frames) == 1:
                        frames_metadata[sample_id] = frames[0]
                        break

                    frames_metadata[sample_id] = frames[cvat_frame_id]

                # Polyline(s) corresponding to instance/semantic masks need to
                # be converted to their final format
                self._convert_polylines_to_masks(
                    label_field_results, label_info, frames_metadata
                )

                annotations = self._merge_results(
                    annotations, {label_field: label_field_results}
                )

        return annotations

    def _get_project_labels(self, project_id):
        if self.get_project_name(project_id) is None:
            raise ValueError("Project '%s' not found" % project_id)

        return self.get(self.project_url(project_id)).json()["labels"]

    def _parse_project_details(self, project_name, project_id):
        if project_id is not None:
            project_name = self.get_project_name(project_id)
            if not project_name:
                raise ValueError("Project '%d' not found" % project_id)

        elif project_name is not None:
            project_id = self.get_project_id(project_name)

        return project_name, project_id

    def _get_cvat_schema(
        self, label_schema, project_id=None, occluded_attr=None
    ):
        if project_id is not None:
            return self._convert_cvat_schema(
                label_schema, project_id, occluded_attr=occluded_attr
            )

        return self._build_cvat_schema(
            label_schema, occluded_attr=occluded_attr
        )

    def _convert_cvat_schema(
        self, label_schema, project_id, occluded_attr=None
    ):
        labels = self._get_project_labels(project_id)

        cvat_schema = {}
        labels_to_update = []
        occluded_attrs = {}
        assign_scalar_attrs = {}
        classes_and_attrs = []
        for label in labels:
            name = label["name"]
            attrs = label["attributes"]
            cvat_schema[name] = {a["name"]: a for a in attrs}

            if "label_id" not in cvat_schema[name]:
                labels_to_update.append(label)
                cvat_schema[name]["label_id"] = {
                    "name": "label_id",
                    "input_type": "text",
                    "mutable": True,
                }

            label_attrs = {}
            for attr_name, attr in cvat_schema[name].items():
                if attr_name != "label_id":
                    input_type = attr["input_type"]
                    label_attrs[attr_name] = {"type": input_type}
                    default_value = attr["default_value"]
                    values = attr["values"]
                    if default_value:
                        label_attrs[attr_name]["default"] = default_value

                    if values and values[0] != "":
                        label_attrs[attr_name]["values"] = values

            if occluded_attr is not None:
                label_attrs[occluded_attr] = {}

            classes_and_attrs.append(
                {"classes": [name], "attributes": label_attrs,}
            )

        label_field_classes = {}
        class_names = {n: n for n in cvat_schema.keys()}
        for label_field, label_info in label_schema.items():
            label_type = label_info.get("type", None)
            classes = label_info.get("classes", [])

            if label_type == "scalar":
                # True: scalars are annotated as tag attributes
                # False: scalars are annotated as tag labels
                assign_scalar_attrs[label_field] = not bool(classes)
            else:
                if label_type is not None:
                    label_schema[label_field]["attributes"] = {}
                    label_schema[label_field]["classes"] = classes_and_attrs

                assign_scalar_attrs[label_field] = None

            label_field_classes[label_field] = deepcopy(class_names)
            if occluded_attr is not None:
                occluded_attrs[label_field] = {
                    c: occluded_attr for c in class_names.keys()
                }

        if labels_to_update:
            self._add_project_label_ids(project_id, list(labels_to_update))

        return (
            cvat_schema,
            assign_scalar_attrs,
            occluded_attrs,
            label_field_classes,
        )

    def _add_project_label_ids(self, project_id, labels):
        labels_patch = {"labels": []}
        for label in labels:
            label["attributes"].append(
                {"name": "label_id", "input_type": "text", "mutable": True}
            )
            labels_patch["labels"].append(label)

        self.patch(self.project_url(project_id), json=labels_patch)

    def _ensure_one_field_per_type(self, label_schema, verbose=True):
        _seen_label_types = []
        for label_field in list(label_schema.keys()):  # list b/c we may edit
            if label_field is None:
                continue

            label_type = label_schema[label_field]["type"]
            if label_type == "scalar":
                _label_type = "classifications"
            else:
                _label_type = foua._RETURN_TYPES_MAP[label_type]

            if _label_type not in _seen_label_types:
                _seen_label_types.append(_label_type)
            elif verbose:
                label_schema.pop(label_field)
                logger.warning(
                    "A field with label type '%s' is already being annotated. "
                    "Ignoring field '%s'...",
                    _label_type,
                    label_field,
                )

    def _get_batch_size(self, samples):
        if samples.media_type == fom.VIDEO:
            # The current implementation (both upload and download) requires
            # frame IDs for all frames that might get labels
            samples.ensure_frames()

            # CVAT only allows for one video per task
            return 1

        samples.compute_metadata()

        # Put all image samples in one task
        return len(samples)

    def _create_task_upload_data(
        self,
        config,
        idx,
        task_name,
        cvat_schema,
        project_id,
        samples_batch,
        task_ids,
        job_ids,
        frame_id_map,
    ):
        media_field = config.media_field
        segment_size = config.segment_size
        image_quality = config.image_quality
        use_cache = config.use_cache
        use_zip_chunks = config.use_zip_chunks
        chunk_size = config.chunk_size
        task_assignee = config.task_assignee
        job_assignees = config.job_assignees
        job_reviewers = config.job_reviewers

        is_video = samples_batch.media_type == fom.VIDEO

        _task_assignee = task_assignee
        _job_assignees = job_assignees
        _job_reviewers = job_reviewers

        if is_video:
            # Videos are uploaded in multiple tasks with 1 job per task
            # Assign the correct users for the current task
            if job_assignees is not None:
                _job_assignees = [job_assignees[idx % len(job_assignees)]]

            if job_reviewers is not None:
                _job_reviewers = [job_reviewers[idx % len(job_reviewers)]]

        if task_assignee is not None:
            if isinstance(task_assignee, str):
                _task_assignee = task_assignee
            else:
                _task_assignee = task_assignee[idx % len(task_assignee)]

        # Create task
        task_id, class_id_map, attr_id_map = self.create_task(
            task_name,
            schema=cvat_schema,
            segment_size=segment_size,
            image_quality=image_quality,
            task_assignee=_task_assignee,
            project_id=project_id,
        )
        task_ids.append(task_id)

        # Upload media
        job_ids[task_id] = self.upload_data(
            task_id,
            samples_batch.values(media_field),
            image_quality=image_quality,
            use_cache=use_cache,
            use_zip_chunks=use_zip_chunks,
            chunk_size=chunk_size,
            job_assignees=_job_assignees,
            job_reviewers=_job_reviewers,
        )
        frame_id_map[task_id] = self._build_frame_id_map(samples_batch)

        return task_id, class_id_map, attr_id_map

    def _upload_annotations(
        self,
        anno_shapes,
        anno_tags,
        anno_tracks,
        class_id_map,
        attr_id_map,
        task_id,
    ):
        # Remap annotations to use the CVAT class/attribute IDs
        anno_shapes = self._remap_ids(anno_shapes, class_id_map, attr_id_map)
        anno_tags = self._remap_ids(anno_tags, class_id_map, attr_id_map)
        anno_tracks = self._remap_track_ids(
            anno_tracks, class_id_map, attr_id_map
        )

        anno_json = {
            "version": 0,
            "shapes": anno_shapes,
            "tags": anno_tags,
            "tracks": anno_tracks,
        }
        num_shapes = len(anno_shapes)
        num_tags = len(anno_tags)
        num_tracks = len(anno_tracks)

        # @todo is this loop really needed?
        num_uploaded_shapes = 0
        num_uploaded_tags = 0
        num_uploaded_tracks = 0
        anno_resp = {}
        while (
            num_uploaded_shapes != num_shapes
            or num_uploaded_tags != num_tags
            or num_uploaded_tracks != num_tracks
        ):
            anno_resp = self.put(
                self.task_annotation_url(task_id), json=anno_json
            ).json()
            num_uploaded_shapes = len(anno_resp["shapes"])
            num_uploaded_tags = len(anno_resp["tags"])
            num_uploaded_tracks = len(anno_resp["tracks"])

        return self._create_server_id_map(anno_resp, attr_id_map)

    def _create_server_id_map(self, anno_resp, attr_id_map):
        label_id_map = {}
        for class_id, class_attr_map in attr_id_map.items():
            for attr_name, attr_id in class_attr_map.items():
                if attr_name == "label_id":
                    label_id_map[class_id] = attr_id

        server_id_map = {}
        for anno_type, anno_list in anno_resp.items():
            if anno_type not in ("tags", "shapes", "tracks"):
                continue

            id_map = {}
            for anno in anno_list:
                server_id = anno["id"]
                label_id = anno["label_id"]
                if label_id in label_id_map:
                    label_attr_id = label_id_map[label_id]
                    for attr in anno["attributes"]:
                        if attr["spec_id"] == label_attr_id:
                            id_map[server_id] = attr["value"]

            server_id_map[anno_type] = id_map

        return server_id_map

    def _update_shapes_tags_tracks(
        self,
        tags,
        shapes,
        tracks,
        id_map,
        label_type,
        samples_batch,
        label_field,
        label_info,
        cvat_schema,
        assign_scalar_attrs,
        only_keyframes,
        occluded_attrs,
    ):
        is_video = samples_batch.media_type == fom.VIDEO

        anno_tags = []
        anno_shapes = []
        anno_tracks = []

        if label_type in ("classification", "classifications", "scalar"):
            # Tag annotations
            _id_map, anno_tags = self._create_shapes_tags_tracks(
                samples_batch,
                label_field,
                label_info,
                cvat_schema,
                assign_scalar_attrs=assign_scalar_attrs,
            )
        elif is_video and label_type != "segmentation":
            # Video track annotations
            (
                _id_map,
                anno_shapes,
                anno_tracks,
            ) = self._create_shapes_tags_tracks(
                samples_batch,
                label_field,
                label_info,
                cvat_schema,
                load_tracks=True,
                only_keyframes=only_keyframes,
                occluded_attrs=occluded_attrs,
            )
        else:
            # Shape annotations
            _id_map, anno_shapes = self._create_shapes_tags_tracks(
                samples_batch,
                label_field,
                label_info,
                cvat_schema,
                occluded_attrs=occluded_attrs,
            )

        id_map[label_field].update(_id_map)
        tags.extend(anno_tags)
        shapes.extend(anno_shapes)
        tracks.extend(anno_tracks)

    def _filter_field_classes(self, tags, shapes, tracks, _cvat_classes):
        _tags = [t for t in tags if t["label_id"] in _cvat_classes]
        _shapes = [s for s in shapes if s["label_id"] in _cvat_classes]
        _tracks = [t for t in tracks if t["label_id"] in _cvat_classes]
        return _tags, _shapes, _tracks

    def _get_return_label_types(self, label_schema, label_fields):
        label_types = []
        for label_field in label_fields:
            label_type = label_schema[label_field].get("type", None)
            if label_type:
                label_types.append(foua._RETURN_TYPES_MAP[label_type])

        return label_types

    def _get_ignored_types(
        self, project_id, label_types, label_type, is_last_field
    ):
        """When uploading multiple fields to an existing project, each field
        must have a different type but can have overlapping class names.
        Therefore, when loading annotations, if a field exists for a found
        label type, that label will not be loaded with any other fields.
        """
        if not project_id or len(label_types) < 2:
            # Not relevant unless uploading to a project and there are multiple
            # types of labels
            return []

        # The last label field being loaded stores all unexpected label types
        # Ignore only the other label types that have been loaded
        label_type = foua._RETURN_TYPES_MAP[label_type]
        if is_last_field:
            ignored_types = set(label_types) - {label_type}
        else:
            # Other fields only load the expected type
            # Ignore all other types
            all_label_types = foua._RETURN_TYPES_MAP.values()
            ignored_types = set(all_label_types) - {label_type}

        return ignored_types

    def _convert_polylines_to_masks(
        self, results, label_info, frames_metadata
    ):
        for label_type, type_results in results.items():
            if label_type not in (
                "detection",
                "detections",
                "instance",
                "instances",
                "segmentation",
            ):
                continue

            for sample_id, sample_results in type_results.items():
                sample_metadata = frames_metadata[sample_id]
                frame_size = (
                    sample_metadata["width"],
                    sample_metadata["height"],
                )
                for _id, _content in sample_results.items():
                    if isinstance(_content, dict):
                        frame_id = _id
                        frame_results = _content
                        for label_id, label in frame_results.items():
                            label = self._convert_polylines(
                                label_id, label, label_info, frame_size
                            )
                            results[label_type][sample_id][frame_id][
                                label_id
                            ] = label
                    else:
                        label_id = _id
                        label = self._convert_polylines(
                            label_id, _content, label_info, frame_size
                        )
                        results[label_type][sample_id][label_id] = label

    def _convert_polylines(self, label_id, label, label_info, frame_size):
        # Convert Polyline to instance segmentation
        if isinstance(label, fol.Polyline):
            detection = CVATShape.polyline_to_detection(label, frame_size)
            detection._id = ObjectId(label_id)
            return detection

        # Convert Polylines to semantic segmentation
        if isinstance(label, fol.Polylines):
            mask_targets = label_info.get("mask_targets", None)
            segmentation = CVATShape.polylines_to_segmentation(
                label, frame_size, mask_targets
            )
            segmentation._id = ObjectId(label_id)
            return segmentation

        return label

    def _merge_results(self, results, new_results):
        if isinstance(new_results, dict):
            for key, val in new_results.items():
                if key not in results:
                    results[key] = val
                else:
                    results[key] = self._merge_results(results[key], val)

        return results

    def _parse_shapes_tags(
        self,
        anno_type,
        annos,
        frame_id_map,
        label_type,
        id_map,
        server_id_map,
        class_map,
        attr_id_map,
        frames,
        ignore_types,
        assigned_scalar_attrs=False,
        track_index=None,
        immutable_attrs=None,
        occluded_attrs=None,
    ):
        results = {}
        prev_type = None

        # For filling in tracked objects
        prev_frame = None
        prev_outside = True

        if anno_type == "track":
            annos = _get_interpolated_shapes(annos)

        for anno in annos:
            frame = anno["frame"]
            prev_anno = anno
            prev_frame = frame
            prev_outside = anno.get("outside", True)

            if anno.get("outside", False):
                # If a tracked object is not in the frame
                continue

            prev_type = self._parse_annotation(
                anno,
                results,
                anno_type,
                prev_type,
                frame_id_map,
                label_type,
                id_map,
                server_id_map,
                class_map,
                attr_id_map,
                frames,
                ignore_types,
                assigned_scalar_attrs=assigned_scalar_attrs,
                track_index=track_index,
                immutable_attrs=immutable_attrs,
                occluded_attrs=occluded_attrs,
            )

        # For non-outside tracked objects, the last track goes to the end of
        # the video, so fill remaining frames with copies of the last instance
        if prev_frame is not None and not prev_outside:
            for frame in range(prev_frame + 1, len(frame_id_map)):
                anno = deepcopy(prev_anno)
                anno["frame"] = frame
                anno["keyframe"] = False

                prev_type = self._parse_annotation(
                    anno,
                    results,
                    anno_type,
                    prev_type,
                    frame_id_map,
                    label_type,
                    id_map,
                    server_id_map,
                    class_map,
                    attr_id_map,
                    frames,
                    ignore_types,
                    assigned_scalar_attrs=assigned_scalar_attrs,
                    track_index=track_index,
                    immutable_attrs=immutable_attrs,
                    occluded_attrs=occluded_attrs,
                )

        return results

    def _parse_annotation(
        self,
        anno,
        results,
        anno_type,
        prev_type,
        frame_id_map,
        expected_label_type,
        id_map,
        server_id_map,
        class_map,
        attr_id_map,
        frames,
        ignore_types,
        assigned_scalar_attrs=False,
        track_index=None,
        immutable_attrs=None,
        occluded_attrs=None,
    ):
        frame = anno["frame"]
        if len(frames) > frame:
            metadata = frames[frame]
        else:
            metadata = frames[0]

        if frame not in frame_id_map:
            return prev_type

        frame_data = frame_id_map[frame]
        sample_id = frame_data["sample_id"]
        frame_id = frame_data.get("frame_id", None)

        label = None

        if anno_type in ("shapes", "track"):
            shape_type = anno["type"]
            keyframe = anno.get("keyframe", False)

            if expected_label_type == "scalar" and assigned_scalar_attrs:
                # Shapes created with values, set class to value
                anno_attrs = anno["attributes"]
                if anno_attrs and "value" in anno_attrs[0]:
                    class_val = anno_attrs[0]["value"]
                    anno["attributes"] = []
                else:
                    class_val = False

            cvat_shape = CVATShape(
                anno,
                class_map,
                attr_id_map,
                server_id_map,
                metadata,
                index=track_index,
                immutable_attrs=immutable_attrs,
                occluded_attrs=occluded_attrs,
            )

            # Non-keyframe annotations were interpolated from keyframes but
            # should not inherit their label IDs
            if anno_type == "track" and not keyframe:
                cvat_shape._id = None

            if shape_type == "rectangle":
                label_type = "detections"
                label = cvat_shape.to_detection()
            elif shape_type == "polygon":
                if expected_label_type == "segmentation":
                    # A piece of a segmentation mask
                    label_type = "segmentation"
                    label = cvat_shape.to_polyline(closed=True, filled=True)
                elif expected_label_type in (
                    "detection",
                    "detections",
                    "instance",
                    "instances",
                ):
                    # A piece of an instance mask
                    label_type = "detections"
                    label = cvat_shape.to_polyline(closed=True, filled=True)
                else:
                    # A regular polyline or polygon
                    if expected_label_type in ("polyline", "polylines"):
                        filled = False
                    else:
                        filled = True

                    label_type = "polylines"
                    label = cvat_shape.to_polyline(closed=True, filled=filled)
            elif shape_type == "polyline":
                label_type = "polylines"
                label = cvat_shape.to_polyline()
            elif shape_type == "points":
                label_type = "keypoints"
                label = cvat_shape.to_keypoint()

            if keyframe:
                label["keyframe"] = True

            if expected_label_type == "scalar" and assigned_scalar_attrs:
                if class_val and label is not None:
                    label.label = class_val

        if anno_type == "tags":
            if expected_label_type == "scalar":
                label_type = "scalar"
                if assigned_scalar_attrs:
                    num_attrs = len(anno["attributes"])
                    attr_ind = 0
                    while label is None and attr_ind < num_attrs:
                        label = _parse_value(
                            anno["attributes"][attr_ind]["value"]
                        )
                        attr_ind += 1
                        if label is not None:
                            if prev_type is str:
                                label = str(label)

                            if prev_type is None:
                                prev_type = type(label)
                            elif not isinstance(label, prev_type):
                                msg = (
                                    "Ignoring scalar of type %s that does not "
                                    "match previously inferred scalar type %s"
                                ) % (type(label), prev_type)
                                warnings.warn(msg)
                                label = None
                else:
                    label = class_map[anno["label_id"]]
            else:
                label_type = "classifications"
                cvat_tag = CVATTag(anno, class_map, attr_id_map, server_id_map)
                label = cvat_tag.to_classification()

        if label is None or label_type in ignore_types:
            return prev_type

        if label_type not in results:
            results[label_type] = {}

        if sample_id not in results[label_type]:
            results[label_type][sample_id] = {}

        if (
            frame_id is not None
            and frame_id not in results[label_type][sample_id]
        ):
            results[label_type][sample_id][frame_id] = {}

        if label_type == "segmentation":
            seg_id = self._get_segmentation_id(id_map, sample_id, frame_id)
        else:
            seg_id = None

        if frame_id is not None:
            if label_type == "scalar":
                results[label_type][sample_id][frame_id] = label
            else:
                _results = results[label_type][sample_id][frame_id]

                self._add_label_to_results(
                    _results, label_type, label, seg_id=seg_id
                )
        else:
            if label_type == "scalar":
                results[label_type][sample_id] = label
            else:
                _results = results[label_type][sample_id]

                self._add_label_to_results(
                    _results, label_type, label, seg_id=seg_id
                )

        return prev_type

    def _get_segmentation_id(self, id_map, sample_id, frame_id):
        _id = id_map.get(sample_id, None)

        if frame_id is not None and isinstance(_id, dict):
            _id = _id.get(frame_id, None)

        if etau.is_str(_id):
            return _id

        if isinstance(_id, list) and len(_id) == 1:
            return _id[0]

        return None

    def _add_label_to_results(self, results, label_type, label, seg_id=None):
        # Merge polylines representing a semantic segmentation
        if label_type == "segmentation":
            if seg_id is None:
                seg_id = str(ObjectId())

            if results:
                polylines = next(iter(results.values()))
            else:
                polylines = fol.Polylines()
                results[seg_id] = polylines

            found_existing_class = False
            for polyline in polylines.polylines:
                if label.label == polyline.label:
                    found_existing_class = True
                    polyline.points.extend(label.points)

            if not found_existing_class:
                polylines.polylines.append(label)

            return

        # Merge polylines representing an instance segmentation
        if label_type == "detections" and isinstance(label, fol.Polyline):
            if label.id in results:
                results[label.id].points.extend(label.points)
            else:
                results[label.id] = label

            return

        results[label.id] = label

    def _parse_arg(self, arg, config_arg):
        if arg is None:
            return config_arg

        return arg

    def _build_cvat_schema(self, label_schema, occluded_attr=None):
        cvat_schema = {}
        assign_scalar_attrs = {}
        occluded_attrs = defaultdict(dict)
        label_field_classes = defaultdict(dict)

        _class_label_fields = {}
        _duplicate_classes = set()
        _prev_field_classes = set()

        for label_field, label_info in label_schema.items():
            _field_classes = set()
            label_type = label_info["type"]
            is_existing_field = label_info["existing_field"]
            classes = label_info["classes"]
            attributes, occluded_attr_name = self._to_cvat_attributes(
                label_info["attributes"]
            )
            if occluded_attr_name is None and occluded_attr is not None:
                occluded_attr_name = occluded_attr
                label_schema[label_field]["attributes"][occluded_attr] = {}

            # Must track label IDs for existing label fields
            if is_existing_field and label_type != "scalar":
                if "label_id" in attributes:
                    raise ValueError(
                        "Label field '%s' attribute schema cannot use "
                        "reserved name 'label_id'" % label_field
                    )

                attributes["label_id"] = {
                    "name": "label_id",
                    "input_type": "text",
                    "mutable": True,
                }

            if label_type == "scalar":
                # True: scalars are annotated as tag attributes
                # False: scalars are annotated as tag labels
                assign_scalar_attrs[label_field] = not bool(classes)
            else:
                assign_scalar_attrs[label_field] = None

            if not classes:
                classes = [label_field]

                if not attributes:
                    attributes["value"] = {
                        "name": "value",
                        "input_type": "text",
                        "mutable": True,
                    }

            # Handle class name clashes and global attributes
            for _class in classes:
                if etau.is_str(_class):
                    _classes = [_class]
                else:
                    _classes = _class["classes"]

                for name in _classes:
                    # If two label fields share a class name, we must append
                    # `label_field` to all instances of `name` to disambiguate
                    if (
                        name in _prev_field_classes
                        and name not in _duplicate_classes
                    ):
                        _duplicate_classes.add(name)

                        prev_field = _class_label_fields[name]

                        new_name = "%s_%s" % (name, prev_field)
                        cvat_schema[new_name] = cvat_schema.pop(name)

                        label_field_classes[prev_field][name] = new_name

                        if name in occluded_attrs[label_field]:
                            attr_name = occluded_attrs[label_field].pop(name)
                            occluded_attrs[label_field][new_name] = attr_name

                    _field_classes.add(name)

                    if name in _duplicate_classes:
                        new_name = "%s_%s" % (name, label_field)
                        label_field_classes[label_field][name] = new_name
                        name = new_name
                    else:
                        _class_label_fields[name] = label_field
                        label_field_classes[label_field][name] = name

                    cvat_schema[name] = deepcopy(attributes)
                    if occluded_attr_name is not None:
                        occluded_attrs[label_field][name] = occluded_attr_name

            _prev_field_classes |= _field_classes

            # Class-specific attributes
            for _class in classes:
                if etau.is_str(_class):
                    continue

                _classes = _class["classes"]
                _attrs, _occluded_attr_name = self._to_cvat_attributes(
                    _class["attributes"]
                )
                if _occluded_attr_name is None and occluded_attr is not None:
                    _occluded_attr_name = occluded_attr

                if "label_id" in _attrs:
                    raise ValueError(
                        "Label field '%s' attribute schema cannot use "
                        "reserved name 'label_id'" % label_field
                    )

                for name in _classes:
                    if name in _duplicate_classes:
                        name = "%s_%s" % (name, label_field)

                    cvat_schema[name].update(_attrs)
                    if _occluded_attr_name is not None:
                        occluded_attrs[label_field][name] = _occluded_attr_name

        return (
            cvat_schema,
            assign_scalar_attrs,
            dict(occluded_attrs),
            dict(label_field_classes),
        )

    def _to_cvat_attributes(self, attributes):
        cvat_attrs = {}
        occluded_attr_name = None
        for attr_name, info in attributes.items():
            cvat_attr = {"name": attr_name, "mutable": True}
            is_occluded = False
            for attr_key, val in info.items():
                if attr_key == "type":
                    if val == "occluded":
                        occluded_attr_name = attr_name
                        is_occluded = True
                    else:
                        cvat_attr["input_type"] = val
                elif attr_key == "values":
                    cvat_attr["values"] = [_stringify_value(v) for v in val]
                elif attr_key == "default":
                    cvat_attr["default_value"] = _stringify_value(val)
                elif attr_key == "mutable":
                    cvat_attr["mutable"] = bool(val)

            if not is_occluded:
                cvat_attrs[attr_name] = cvat_attr

        return cvat_attrs, occluded_attr_name

    def _create_shapes_tags_tracks(
        self,
        samples,
        label_field,
        label_info,
        cvat_schema,
        assign_scalar_attrs=False,
        load_tracks=False,
        only_keyframes=False,
        occluded_attrs=None,
    ):
        label_type = label_info["type"]
        classes = label_info["classes"]
        mask_targets = label_info.get("mask_targets", None)

        if occluded_attrs is not None:
            occluded_attrs = occluded_attrs.get(label_field, None)

        id_map = {}
        tags_or_shapes = []
        tracks = {}

        # Tracks any "attribute:" prefixes that need to be prepended to
        # attributes in `cvat_schema` because the corresponding data is found
        # to be in the attributes dict of the FiftyOne labels
        remapped_attrs = {}

        is_video = samples.media_type == fom.VIDEO

        if is_video:
            field, _ = samples._handle_frame_field(label_field)
        else:
            field = label_field

        frame_id = -1
        for sample in samples:
            metadata = sample.metadata

            if is_video:
                images = sample.frames.values()
                frame_size = (metadata.frame_width, metadata.frame_height)
            else:
                images = [sample]
                frame_size = (metadata.width, metadata.height)

            for image in images:
                frame_id += 1

                label = image[field]

                if label is None:
                    continue

                kwargs = {}

                if label_type not in (
                    "scalar",
                    "classification",
                    "classifications",
                    "segmentation",
                ):
                    kwargs["load_tracks"] = load_tracks
                    kwargs["occluded_attrs"] = occluded_attrs

                if label_type == "scalar":
                    labels = label
                    kwargs["assign_scalar_attrs"] = assign_scalar_attrs
                    func = self._create_scalar_tags
                elif label_type == "classification":
                    labels = [label]
                    func = self._create_classification_tags
                elif label_type == "classifications":
                    labels = label.classifications
                    func = self._create_classification_tags
                elif label_type in ("detection", "instance"):
                    labels = [label]
                    func = self._create_detection_shapes
                elif label_type in ("detections", "instances"):
                    labels = label.detections
                    func = self._create_detection_shapes
                elif label_type in ("polyline", "polygon"):
                    labels = [label]
                    func = self._create_polyline_shapes
                elif label_type in ("polylines", "polygons"):
                    labels = label.polylines
                    func = self._create_polyline_shapes
                elif label_type == "keypoint":
                    labels = [label]
                    func = self._create_keypoint_shapes
                elif label_type == "keypoints":
                    labels = label.keypoints
                    func = self._create_keypoint_shapes
                elif label_type == "segmentation":
                    labels = label
                    func = self._create_segmentation_shapes
                    kwargs["mask_targets"] = mask_targets
                else:
                    raise ValueError(
                        "Label type '%s' of field '%s' is not supported"
                        % (label_type, label_field)
                    )

                ids, _tags_or_shapes, _tracks, _remapped_attrs = func(
                    labels,
                    cvat_schema,
                    label_field,
                    frame_id,
                    frame_size,
                    label_type=label_type,
                    **kwargs,
                )

                tags_or_shapes.extend(_tags_or_shapes)
                self._merge_tracks(tracks, _tracks)
                remapped_attrs.update(_remapped_attrs)

                if ids is not None:
                    if is_video:
                        if sample.id not in id_map:
                            id_map[sample.id] = {}

                        id_map[sample.id][image.id] = ids
                    else:
                        id_map[sample.id] = ids

        # Record any attribute name changes due to label attributes being
        # stored in attributes dicts rather than as dynamic fields
        for attr_schema in cvat_schema.values():
            for name, attr in attr_schema.items():
                if name in remapped_attrs:
                    attr["name"] = remapped_attrs[name]

        if load_tracks:
            tracks = self._finalize_tracks(tracks, frame_id, only_keyframes)
            return id_map, tags_or_shapes, tracks

        return id_map, tags_or_shapes

    def _create_scalar_tags(
        self,
        label,
        cvat_schema,
        label_field,
        frame_id,
        frame_size,
        label_type=None,
        assign_scalar_attrs=False,
    ):
        if label is None:
            label = ""

        if assign_scalar_attrs[label_field]:
            if label_field not in cvat_schema:
                return False, [], {}, {}

            scalar_attr_name = next(iter(cvat_schema[label_field].keys()))

            class_name = label_field
            attributes = [
                {
                    "spec_id": scalar_attr_name,
                    "value": _stringify_value(label),
                }
            ]
        else:
            class_name = _stringify_value(label)
            if class_name not in cvat_schema:
                return False, [], {}, {}

            attributes = []

        tags = [
            {
                "label_id": class_name,
                "group": 0,
                "frame": frame_id,
                "source": "manual",
                "attributes": attributes,
            }
        ]

        return True, tags, {}, {}

    def _create_classification_tags(
        self,
        classifications,
        cvat_schema,
        label_field,
        frame_id,
        frame_size,
        label_type=None,
    ):
        ids = []
        tags = []
        remapped_attrs = {}

        for cn in classifications:
            (
                class_name,
                attributes,
                _,
                _remapped_attrs,
                _,
            ) = self._parse_label(cn, cvat_schema, label_field)

            if class_name is None:
                continue

            ids.append(cn.id)
            remapped_attrs.update(_remapped_attrs)
            tags.append(
                {
                    "label_id": class_name,
                    "group": 0,
                    "frame": frame_id,
                    "source": "manual",
                    "attributes": attributes,
                }
            )

        if label_type == "classification":
            ids = ids[0] if ids else None

        return ids, tags, {}, remapped_attrs

    def _create_detection_shapes(
        self,
        detections,
        cvat_schema,
        label_field,
        frame_id,
        frame_size,
        label_type=None,
        label_id=None,
        load_tracks=False,
        occluded_attrs=None,
    ):
        ids = []
        shapes = []
        tracks = {}
        remapped_attrs = {}

        for det in detections:
            (
                class_name,
                attributes,
                immutable_attrs,
                _remapped_attrs,
                is_occluded,
            ) = self._parse_label(
                det,
                cvat_schema,
                label_field,
                label_id=label_id,
                occluded_attrs=occluded_attrs,
            )

            if class_name is None:
                continue

            curr_shapes = []

            if label_type in ("detection", "detections"):
                x, y, w, h = det.bounding_box
                width, height = frame_size
                xtl = float(round(x * width))
                ytl = float(round(y * height))
                xbr = float(round((x + w) * width))
                ybr = float(round((y + h) * height))
                bbox = [xtl, ytl, xbr, ybr]

                curr_shapes.append(
                    {
                        "type": "rectangle",
                        "occluded": is_occluded,
                        "z_order": 0,
                        "points": bbox,
                        "label_id": class_name,
                        "group": 0,
                        "frame": frame_id,
                        "source": "manual",
                        "attributes": attributes,
                    }
                )
            elif label_type in ("instance", "instances"):
                if det.mask is None:
                    continue

                polygon = det.to_polyline()
                for points in polygon.points:
                    if len(points) < 3:
                        continue  # CVAT polygons must contain >= 3 points

                    abs_points = HasCVATPoints._to_abs_points(
                        points, frame_size
                    )
                    flattened_points = list(
                        itertools.chain.from_iterable(abs_points)
                    )

                    curr_shapes.append(
                        {
                            "type": "polygon",
                            "occluded": is_occluded,
                            "z_order": 0,
                            "points": flattened_points,
                            "label_id": class_name,
                            "group": 0,
                            "frame": frame_id,
                            "source": "manual",
                            "attributes": deepcopy(attributes),
                        }
                    )

            if not curr_shapes:
                continue

            ids.append(det.id)
            remapped_attrs.update(_remapped_attrs)

            if load_tracks and det.index is not None:
                keyframe = det.get_attribute_value("keyframe", False)
                self._add_shapes_to_tracks(
                    tracks,
                    curr_shapes,
                    class_name,
                    det.index,
                    frame_id,
                    immutable_attrs,
                    keyframe,
                )
            else:
                shapes.extend(curr_shapes)

        return ids, shapes, tracks, remapped_attrs

    def _create_keypoint_shapes(
        self,
        keypoints,
        cvat_schema,
        label_field,
        frame_id,
        frame_size,
        label_type=None,
        load_tracks=False,
        occluded_attrs=None,
    ):
        ids = []
        shapes = []
        tracks = {}
        remapped_attrs = {}

        for kp in keypoints:
            (
                class_name,
                attributes,
                immutable_attrs,
                _remapped_attrs,
                is_occluded,
            ) = self._parse_label(
                kp, cvat_schema, label_field, occluded_attrs=occluded_attrs
            )

            if class_name is None:
                continue

            abs_points = HasCVATPoints._to_abs_points(kp.points, frame_size)
            flattened_points = list(itertools.chain.from_iterable(abs_points))

            shape = {
                "type": "points",
                "occluded": is_occluded,
                "z_order": 0,
                "points": flattened_points,
                "label_id": class_name,
                "group": 0,
                "frame": frame_id,
                "source": "manual",
                "attributes": attributes,
            }

            ids.append(kp.id)
            remapped_attrs.update(_remapped_attrs)

            if load_tracks and kp.index is not None:
                keyframe = kp.get_attribute_value("keyframe", False)
                self._add_shapes_to_tracks(
                    tracks,
                    [shape],
                    class_name,
                    kp.index,
                    frame_id,
                    immutable_attrs,
                    keyframe,
                )
            else:
                shapes.append(shape)

        return ids, shapes, tracks, remapped_attrs

    def _create_polyline_shapes(
        self,
        polylines,
        cvat_schema,
        label_field,
        frame_id,
        frame_size,
        label_type=None,
        load_tracks=False,
        occluded_attrs=None,
    ):
        ids = []
        shapes = []
        tracks = {}
        remapped_attrs = {}

        for poly in polylines:
            (
                class_name,
                attributes,
                immutable_attrs,
                _remapped_attrs,
                is_occluded,
            ) = self._parse_label(
                poly, cvat_schema, label_field, occluded_attrs=occluded_attrs
            )

            if class_name is None:
                continue

            curr_shapes = []

            for points in poly.points:
                if poly.filled and len(points) < 3:
                    continue  # CVAT polygons must contain >= 3 points

                abs_points = HasCVATPoints._to_abs_points(points, frame_size)
                flattened_points = list(
                    itertools.chain.from_iterable(abs_points)
                )

                shape = {
                    "type": "polygon" if poly.filled else "polyline",
                    "occluded": is_occluded,
                    "z_order": 0,
                    "points": flattened_points,
                    "label_id": class_name,
                    "group": 0,
                    "frame": frame_id,
                    "source": "manual",
                    "attributes": deepcopy(attributes),
                }
                curr_shapes.append(shape)

            if not curr_shapes:
                continue

            ids.append(poly.id)
            remapped_attrs.update(_remapped_attrs)

            if load_tracks and poly.index is not None:
                keyframe = poly.get_attribute_value("keyframe", False)
                self._add_shapes_to_tracks(
                    tracks,
                    curr_shapes,
                    class_name,
                    poly.index,
                    frame_id,
                    immutable_attrs,
                    keyframe,
                )
            else:
                shapes.extend(curr_shapes)

        return ids, shapes, tracks, remapped_attrs

    def _create_segmentation_shapes(
        self,
        segmentation,
        cvat_schema,
        label_field,
        frame_id,
        frame_size,
        label_type=None,
        mask_targets=None,
    ):
        label_id = segmentation.id
        detections = segmentation.to_detections(mask_targets=mask_targets)

        _, shapes, tracks, remapped_attrs = self._create_detection_shapes(
            detections.detections,
            cvat_schema,
            label_field,
            frame_id,
            frame_size,
            label_type="instances",
            label_id=label_id,
        )

        return label_id, shapes, tracks, remapped_attrs

    def _parse_label(
        self,
        label,
        cvat_schema,
        label_field,
        label_id=None,
        occluded_attrs=None,
    ):
        # If the class is a duplicate, it will have this name
        dup_class_name = "%s_%s" % (label.label, label_field)

        if label.label in cvat_schema:
            class_name = label.label
        elif dup_class_name in cvat_schema:
            class_name = dup_class_name
        else:
            return None, None, None, None, None

        attr_schema = cvat_schema[class_name]

        if label_id is None:
            label_id = label.id

        label_attrs = [{"spec_id": "label_id", "value": label_id}]
        immutable_attrs = []
        remapped_attrs = {}

        for name, attr in attr_schema.items():
            if name.startswith("attribute:"):
                name = name[len("attribute:") :]

            value = label.get_attribute_value(name, None)
            if value is None:
                continue

            if name not in label:
                # Found attribute stored in the label's attributes dict
                new_name = "attribute:" + name
                remapped_attrs[name] = new_name
                name = new_name

            attr_dict = {"spec_id": name, "value": _stringify_value(value)}

            if attr["mutable"]:
                label_attrs.append(attr_dict)
            else:
                immutable_attrs.append(attr_dict)

        is_occluded = False
        if occluded_attrs is not None:
            attr_name = occluded_attrs.get(class_name, None)
            if attr_name is not None:
                is_occluded = _parse_occlusion_value(
                    label.get_attribute_value(attr_name, False)
                )

        return (
            class_name,
            label_attrs,
            immutable_attrs,
            remapped_attrs,
            is_occluded,
        )

    def _add_shapes_to_tracks(
        self,
        tracks,
        shapes,
        class_name,
        index,
        frame_id,
        immutable_attrs,
        keyframe,
    ):
        if class_name not in tracks:
            tracks[class_name] = {}

        if index not in tracks[class_name]:
            tracks[class_name][index] = {
                "label_id": class_name,
                "shapes": [],
                "frame": frame_id,
                "group": 0,
                "attributes": immutable_attrs,
            }

        _shapes = tracks[class_name][index]["shapes"]

        for shape in shapes:
            shape["outside"] = False
            shape["keyframe"] = keyframe
            del shape["label_id"]
            _shapes.append(shape)

    def _merge_tracks(self, tracks, new_tracks):
        for class_name, class_tracks in new_tracks.items():
            if class_name not in tracks:
                tracks[class_name] = class_tracks
                continue

            for index, track in class_tracks.items():
                if index not in tracks[class_name]:
                    tracks[class_name][index] = track
                else:
                    _track = tracks[class_name][index]
                    _track["shapes"].extend(track["shapes"])
                    _track["frame"] = max(track["frame"], _track["frame"])

    def _finalize_tracks(self, tracks, frame_count, only_keyframes):
        formatted_tracks = []
        for class_tracks in tracks.values():
            for track in class_tracks.values():
                formatted_track = self._finalize_track(
                    track, frame_count, only_keyframes
                )
                formatted_tracks.append(track)

        return formatted_tracks

    def _finalize_track(self, track, frame_count, only_keyframes):
        shapes = track["shapes"]
        new_shapes = []
        prev_frame_shape_inds = []
        prev_frame = None
        next_is_keyframe = True

        for ind, shape in enumerate(shapes):
            frame = shape["frame"]
            if prev_frame is None:
                prev_frame = frame

            if frame != prev_frame:
                if only_keyframes and next_is_keyframe:
                    # The first frame of a new segment is always a keyframe
                    next_is_keyframe = False
                    for ind in prev_frame_shape_inds:
                        shapes[ind]["keyframe"] = True

                # If there is a gap between shapes, we must mark the end of the
                # previous segment as "outside"
                if frame > prev_frame + 1:
                    for prev_ind in prev_frame_shape_inds:
                        last_shape = shapes[prev_ind]
                        new_shape = deepcopy(last_shape)
                        new_shape["frame"] += 1
                        new_shape["outside"] = True
                        if only_keyframes:
                            new_shape["keyframe"] = True

                        new_shapes.append(
                            (max(prev_frame_shape_inds), new_shape)
                        )
                        next_is_keyframe = True

                prev_frame_shape_inds = []
                prev_frame = frame

            prev_frame_shape_inds.append(ind)

        # The shapes in the last frame in the track must be set to "outside"
        last_shape = shapes[-1]
        if last_shape["frame"] < frame_count:
            new_shape = deepcopy(last_shape)
            new_shape["frame"] += 1
            new_shape["outside"] = True
            if only_keyframes:
                new_shape["keyframe"] = True

            new_shapes.append((len(shapes), new_shape))

        # Insert new shapes into track
        for ind, shape in new_shapes[::-1]:
            shapes.insert(ind, shape)

        # Remove non-keyframes if necessary
        if only_keyframes:
            track["shapes"] = [s for s in shapes if s["keyframe"]]

        return track

    def _build_frame_id_map(self, samples):
        is_video = samples.media_type == fom.VIDEO
        frame_id = -1

        frame_id_map = {}
        for sample in samples:
            if is_video:
                images = sample.frames.values()
            else:
                images = [sample]

            for image in images:
                frame_id += 1
                frame_id_map[frame_id] = {"sample_id": sample.id}
                if is_video:
                    frame_id_map[frame_id]["frame_id"] = image.id

        return frame_id_map

    def _remap_ids(self, shapes_or_tags, class_id_map, attr_id_map):
        for obj in shapes_or_tags:
            label_name = obj["label_id"]
            class_id = class_id_map[label_name]
            obj["label_id"] = class_id
            attr_map = attr_id_map[class_id]
            attrs = []
            for attr in obj["attributes"]:
                attr_name = attr["spec_id"]
                if attr_name in attr_map:
                    attr["spec_id"] = attr_map[attr_name]
                    attrs.append(attr)

            obj["attributes"] = attrs

        return shapes_or_tags

    def _remap_track_ids(self, tracks, class_id_map, attr_id_map):
        for track in tracks:
            label_name = track["label_id"]
            class_id = class_id_map[label_name]
            track["label_id"] = class_id
            attr_map = attr_id_map[class_id]
            for shape in track["shapes"]:
                attrs = []
                for attr in shape["attributes"]:
                    attr_name = attr["spec_id"]
                    if attr_name in attr_map:
                        attr["spec_id"] = attr_map[attr_name]
                        attrs.append(attr)

                shape["attributes"] = attrs

            attrs = []
            for attr in track["attributes"]:
                attr_name = attr["spec_id"]
                if attr_name in attr_map:
                    attr["spec_id"] = attr_map[attr_name]
                    attrs.append(attr)

            track["attributes"] = attrs

        return tracks

    def _validate(self, response, kwargs):
        try:
            response.raise_for_status()
        except:
            d = response.__dict__
            logger.info("Arguments the caused this error were:")
            logger.info(kwargs)
            raise Exception(
                "%d error for request %s to url %s with the reason %s. Error "
                "content: %s"
                % (
                    d["status_code"],
                    d["request"],
                    d["url"],
                    d["reason"],
                    d["_content"],
                )
            )


class CVATLabel(object):
    """A label returned by the CVAT API.

    Args:
        label_dict: the dictionary containing the label information loaded from
            the CVAT API
        class_map: a dictionary mapping label IDs to class strings
        attr_id_map: a dictionary mapping attribute IDs attribute names for
            every label
        server_id_map: a dictionary mapping server IDs to FiftyOne label IDs
        attributes (None): an optional list of additional attributes
    """

    def __init__(
        self,
        label_dict,
        class_map,
        attr_id_map,
        server_id_map,
        attributes=None,
    ):
        cvat_id = label_dict["label_id"]
        server_id = label_dict["id"]
        attrs = label_dict["attributes"]

        if attributes is not None:
            attrs.extend(attributes)

        self._id = None
        self.label = class_map[cvat_id]
        self.attributes = {}
        self.fo_attributes = {}

        # Parse attributes
        attr_id_map_rev = {v: k for k, v in attr_id_map[cvat_id].items()}
        for attr in attrs:
            name = attr_id_map_rev[attr["spec_id"]]
            value = _parse_value(attr["value"])
            if value is not None:
                if name.startswith("attribute:"):
                    name = name[len("attribute:") :]
                    fo_attr = CVATAttribute(name, value).to_attribute()
                    self.fo_attributes[name] = fo_attr
                else:
                    self.attributes[name] = value

        # Parse label ID
        label_id = self.attributes.pop("label_id", None)

        if label_id is not None:
            self._set_id(label_id)

        if self._id is None:
            label_id = server_id_map.get(server_id, None)
            if label_id is not None:
                self._set_id(label_id)

    def _set_id(self, label_id):
        try:
            self._id = ObjectId(label_id)
        except:
            pass

    def _set_attributes(self, label):
        if self._id is not None:
            label._id = self._id

        for name, value in self.attributes.items():
            label[name] = value

        if self.fo_attributes:
            label.attributes = self.fo_attributes


class CVATShape(CVATLabel):
    """A shape returned by the CVAT API.

    Args:
        label_dict: the dictionary containing the label information loaded from
            the CVAT API
        class_map: a dictionary mapping label IDs to class strings
        attr_id_map: a dictionary mapping attribute IDs attribute names for
            every label
        server_id_map: a dictionary mapping server IDs to FiftyOne label IDs
        metadata: a dictionary containing the width and height of the frame
        index (None): the tracking index of the shape
        immutable_attrs (None): immutable attributes inherited by this shape
            from its track
        occluded_attrs (None): a dictonary mapping class names to the
            corresponding attribute linked to the CVAT occlusion widget, if any
    """

    def __init__(
        self,
        label_dict,
        class_map,
        attr_id_map,
        server_id_map,
        metadata,
        index=None,
        immutable_attrs=None,
        occluded_attrs=None,
    ):
        super().__init__(
            label_dict,
            class_map,
            attr_id_map,
            server_id_map,
            attributes=immutable_attrs,
        )

        self.frame_size = (metadata["width"], metadata["height"])
        self.points = label_dict["points"]
        self.index = index

        # Parse occluded attribute, if necessary
        if occluded_attrs is not None:
            occluded_attr_name = occluded_attrs.get(self.label, None)
            if occluded_attr_name:
                self.attributes[occluded_attr_name] = label_dict["occluded"]

    def _to_pairs_of_points(self, points):
        reshaped_points = np.reshape(points, (-1, 2))
        return reshaped_points.tolist()

    def to_detection(self):
        """Converts this shape to a :class:`fiftyone.core.labels.Detection`.

        Returns:
            a :class:`fiftyone.core.labels.Detection`
        """
        xtl, ytl, xbr, ybr = self.points
        width, height = self.frame_size
        bbox = [
            xtl / width,
            ytl / height,
            (xbr - xtl) / width,
            (ybr - ytl) / height,
        ]
        label = fol.Detection(
            label=self.label, bounding_box=bbox, index=self.index
        )
        self._set_attributes(label)
        return label

    def to_polyline(self, closed=False, filled=False):
        """Converts this shape to a :class:`fiftyone.core.labels.Polyline`.

        Returns:
            a :class:`fiftyone.core.labels.Polyline`
        """
        points = self._to_pairs_of_points(self.points)
        rel_points = HasCVATPoints._to_rel_points(points, self.frame_size)
        label = fol.Polyline(
            label=self.label,
            points=[rel_points],
            index=self.index,
            closed=closed,
            filled=filled,
        )
        self._set_attributes(label)
        return label

    def to_polylines(self, closed=False, filled=False):
        """Converts this shape to a :class:`fiftyone.core.labels.Polylines`.

        Returns:
            a :class:`fiftyone.core.labels.Polylines`
        """
        points = self._to_pairs_of_points(self.points)
        rel_points = HasCVATPoints._to_rel_points(points, self.frame_size)
        polyline = fol.Polyline(
            label=self.label,
            points=[rel_points],
            closed=closed,
            filled=filled,
        )
        label = fol.Polylines(polylines=[polyline])
        self._set_attributes(label)
        return label

    def to_keypoint(self):
        """Converts this shape to a :class:`fiftyone.core.labels.Keypoint`.

        Returns:
            a :class:`fiftyone.core.labels.Keypoint`
        """
        points = self._to_pairs_of_points(self.points)
        rel_points = HasCVATPoints._to_rel_points(points, self.frame_size)
        label = fol.Keypoint(
            label=self.label, points=rel_points, index=self.index
        )
        self._set_attributes(label)
        return label

    @classmethod
    def polyline_to_detection(cls, polyline, frame_size):
        """Converts a :class:`fiftyone.core.labels.Polyline` to a
        :class:`fiftyone.core.labels.Detection` with a segmentation mask.

        Args:
            polyline: a :class:`fiftyone.core.labels.Polyline`
            frame_size: the ``(width, height)`` of the frame

        Returns:
            a :class:`fiftyone.core.labels.Detection`
        """
        detection = polyline.to_detection(frame_size=frame_size)
        detection._id = polyline._id
        return detection

    @classmethod
    def polylines_to_segmentation(cls, polylines, frame_size, mask_targets):
        """Converts a :class:`fiftyone.core.labels.Polylines` to a
        :class:`fiftyone.core.labels.Segmentation`.

        Args:
            polylines: a :class:`fiftyone.core.labels.Polylines`
            mask_targets: a dict mapping integer pixel values to label strings
            frame_size: the ``(width, height)`` of the frame

        Returns:
            a :class:`fiftyone.core.labels.Segmentation`
        """
        return polylines.to_segmentation(
            frame_size=frame_size, mask_targets=mask_targets
        )


class CVATTag(CVATLabel):
    """A tag returned by the CVAT API.

    Args:
        label_dict: the dictionary containing the label information loaded from
            the CVAT API
        class_map: a dictionary mapping label IDs to class strings
        attr_id_map: a dictionary mapping attribute IDs attribute names for
            every label
        server_id_map: a dictionary mapping server IDs to FiftyOne label IDs
        attributes (None): an optional list of additional attributes
    """

    def to_classification(self):
        """Converts the tag to a :class:`fiftyone.core.labels.Classification`.

        Returns:
            a :class:`fiftyone.core.labels.Classification`
        """
        label = fol.Classification(label=self.label)
        self._set_attributes(label)
        return label


def load_cvat_image_annotations(xml_path):
    """Loads the CVAT image annotations from the given XML file.

    See :ref:`this page <CVATImageDataset-import>` for format details.

    Args:
        xml_path: the path to the annotations XML file

    Returns:
        a tuple of

        -   **info**: a dict of dataset info
        -   **cvat_task_labels**: a :class:`CVATTaskLabels` instance
        -   **cvat_images**: a list of :class:`CVATImage` instances
    """
    d = fou.load_xml_as_json_dict(xml_path)
    annotations = d.get("annotations", {})

    # Verify version
    version = annotations.get("version", None)
    if version is None:
        logger.warning("No version tag found; assuming version 1.1")
    elif version != "1.1":
        logger.warning(
            "Only version 1.1 is explicitly supported; found %s. Trying to "
            "load assuming version 1.1 format",
            version,
        )

    # Load meta
    meta = annotations.get("meta", {})

    # Load task labels
    task = meta.get("task", {})
    labels_dict = task.get("labels", {})
    cvat_task_labels = CVATTaskLabels.from_labels_dict(labels_dict)

    # Load annotations
    image_dicts = _ensure_list(annotations.get("image", []))
    cvat_images = [CVATImage.from_image_dict(id) for id in image_dicts]

    # Load dataset info
    info = {"task_labels": cvat_task_labels.labels}
    if "created" in task:
        info["created"] = task["created"]

    if "updated" in task:
        info["updated"] = task["updated"]

    if "dumped" in meta:
        info["dumped"] = meta["dumped"]

    return info, cvat_task_labels, cvat_images


def load_cvat_video_annotations(xml_path):
    """Loads the CVAT video annotations from the given XML file.

    See :ref:`this page <CVATVideoDataset-import>` for format details.

    Args:
        xml_path: the path to the annotations XML file

    Returns:
        a tuple of

        -   **info**: a dict of dataset info
        -   **cvat_task_labels**: a :class:`CVATTaskLabels` instance
        -   **cvat_tracks**: a list of :class:`CVATTrack` instances
    """
    d = fou.load_xml_as_json_dict(xml_path)
    annotations = d.get("annotations", {})

    # Verify version
    version = annotations.get("version", None)
    if version is None:
        logger.warning("No version tag found; assuming version 1.1")
    elif version != "1.1":
        logger.warning(
            "Only version 1.1 is explicitly supported; found %s. Trying to "
            "load assuming version 1.1 format",
            version,
        )

    # Load meta
    meta = annotations.get("meta", {})

    # Load task labels
    task = meta.get("task", {})
    labels_dict = task.get("labels", {})
    cvat_task_labels = CVATTaskLabels.from_labels_dict(labels_dict)

    # Load annotations
    track_dicts = _ensure_list(annotations.get("track", []))
    if track_dicts:
        original_size = task["original_size"]
        frame_size = (
            int(original_size["width"]),
            int(original_size["height"]),
        )
        cvat_tracks = [
            CVATTrack.from_track_dict(td, frame_size) for td in track_dicts
        ]
    else:
        cvat_tracks = []

    # Load dataset info
    info = {"task_labels": cvat_task_labels.labels}
    if "created" in task:
        info["created"] = task["created"]

    if "updated" in task:
        info["updated"] = task["updated"]

    if "dumped" in meta:
        info["dumped"] = meta["dumped"]

    return info, cvat_task_labels, cvat_tracks


def _is_supported_attribute_type(value):
    return (
        isinstance(value, bool) or etau.is_str(value) or etau.is_numeric(value)
    )


def _cvat_tracks_to_frames_dict(cvat_tracks):
    frames = defaultdict(dict)
    for cvat_track in cvat_tracks:
        labels = cvat_track.to_labels()
        for frame_number, label in labels.items():
            frame = frames[frame_number]

            if isinstance(label, fol.Detection):
                if "detections" not in frame:
                    frame["detections"] = fol.Detections()

                frame["detections"].detections.append(label)
            elif isinstance(label, fol.Polyline):
                if "polylines" not in frame:
                    frame["polylines"] = fol.Polylines()

                frame["polylines"].polylines.append(label)
            elif isinstance(label, fol.Keypoint):
                if "keypoints" not in frame:
                    frame["keypoints"] = fol.Keypoints()

                frame["keypoints"].keypoints.append(label)

    return frames


def _frames_to_cvat_tracks(frames, frame_size):
    labels_map = defaultdict(dict)
    no_index_map = defaultdict(list)
    found_label = False

    def process_label(label, frame_number):
        if label.index is not None:
            labels_map[label.index][frame_number] = label
        else:
            no_index_map[frame_number].append(label)

    # Convert from per-frame to per-object tracks
    for frame_number, frame_dict in frames.items():
        for _, value in frame_dict.items():
            if isinstance(value, (fol.Detection, fol.Polyline, fol.Keypoint)):
                found_label = True
                process_label(value, frame_number)
            elif isinstance(value, fol.Detections):
                found_label = True
                for detection in value.detections:
                    process_label(detection, frame_number)
            elif isinstance(value, fol.Polylines):
                found_label = True
                for polyline in value.polylines:
                    process_label(polyline, frame_number)
            elif isinstance(value, fol.Keypoints):
                found_label = True
                for keypoint in value.keypoints:
                    process_label(keypoint, frame_number)
            elif value is not None:
                msg = "Ignoring unsupported label type '%s'" % value.__class__
                warnings.warn(msg)

    if not found_label:
        return None  # unlabeled

    cvat_tracks = []

    # Generate object tracks
    max_index = -1
    for index in sorted(labels_map):
        max_index = max(index, max_index)
        labels = labels_map[index]
        cvat_track = CVATTrack.from_labels(index, labels, frame_size)
        cvat_tracks.append(cvat_track)

    # Generate single tracks for detections with no `index`
    index = max_index
    for frame_number, labels in no_index_map.items():
        for label in labels:
            index += 1
            cvat_track = CVATTrack.from_labels(
                index, {frame_number: label}, frame_size
            )
            cvat_tracks.append(cvat_track)

    return cvat_tracks


def _get_single_polyline_points(polyline):
    num_polylines = len(polyline.points)
    if num_polylines == 0:
        return []

    if num_polylines > 0:
        msg = (
            "Found polyline with more than one shape; only the first shape "
            "will be stored in CVAT format"
        )
        warnings.warn(msg)

    return polyline.points[0]


def _ensure_list(value):
    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]


def _stringify_value(value):
    if value is None:
        return ""

    if value is True:
        return "true"

    if value is False:
        return "false"

    return str(value)


def _to_int_bool(value):
    return int(bool(value))


def _from_int_bool(value):
    try:
        return bool(int(value))
    except:
        pass

    return None


def _parse_value(value):
    if value in (None, "None", ""):
        return None

    if value in {"True", "true"}:
        return True

    if value in {"False", "false"}:
        return False

    try:
        return int(value)
    except:
        pass

    try:
        return float(value)
    except:
        pass

    return value


def _parse_occlusion_value(value):
    if isinstance(value, bool):
        return value

    if etau.is_str(value):
        str_value = "'%s'" % value
        bool_value = False if value.lower() == "false" else bool(value)
    else:
        str_value = str(value)
        bool_value = bool(value)

    msg = "Casting occlusion value %s of type %s to boolean %s" % (
        str_value,
        type(value),
        bool_value,
    )
    warnings.warn(msg)

    return bool_value


# Track interpolation code sourced from CVAT:
# https://github.com/openvinotoolkit/cvat/blob/31f6234b0cdc656c9dde4294c1008560611c6978/cvat/apps/dataset_manager/annotation.py#L431-L730
def _get_interpolated_shapes(track_shapes):
    def copy_shape(source, frame, points=None):
        copied = deepcopy(source)
        copied["keyframe"] = False
        copied["frame"] = frame
        if points is not None:
            copied["points"] = points
        return copied

    def simple_interpolation(shape0, shape1):
        shapes = []
        distance = shape1["frame"] - shape0["frame"]
        diff = np.subtract(shape1["points"], shape0["points"])

        for frame in range(shape0["frame"] + 1, shape1["frame"]):
            offset = (frame - shape0["frame"]) / distance
            points = shape0["points"] + diff * offset

            shapes.append(copy_shape(shape0, frame, points.tolist()))

        return shapes

    def points_interpolation(shape0, shape1):
        if len(shape0["points"]) == 2 and len(shape1["points"]) == 2:
            return simple_interpolation(shape0, shape1)
        else:
            shapes = []
            for frame in range(shape0["frame"] + 1, shape1["frame"]):
                shapes.append(copy_shape(shape0, frame))

        return shapes

    def interpolate_position(left_position, right_position, offset):
        def to_array(points):
            return np.asarray(
                list(map(lambda point: [point["x"], point["y"]], points))
            ).flatten()

        def to_points(array):
            return list(
                map(
                    lambda point: {"x": point[0], "y": point[1]},
                    np.asarray(array).reshape(-1, 2),
                )
            )

        def curve_length(points):
            length = 0
            for i in range(1, len(points)):
                dx = points[i]["x"] - points[i - 1]["x"]
                dy = points[i]["y"] - points[i - 1]["y"]
                length += np.sqrt(dx ** 2 + dy ** 2)
            return length

        def curve_to_offset_vec(points, length):
            offset_vector = [0]
            accumulated_length = 0
            for i in range(1, len(points)):
                dx = points[i]["x"] - points[i - 1]["x"]
                dy = points[i]["y"] - points[i - 1]["y"]
                accumulated_length += np.sqrt(dx ** 2 + dy ** 2)
                offset_vector.append(accumulated_length / length)

            return offset_vector

        def find_nearest_pair(value, curve):
            minimum = [0, abs(value - curve[0])]
            for i in range(1, len(curve)):
                distance = abs(value - curve[i])
                if distance < minimum[1]:
                    minimum = [i, distance]

            return minimum[0]

        def match_left_right(left_curve, right_curve):
            matching = {}
            for i, left_curve_item in enumerate(left_curve):
                matching[i] = [find_nearest_pair(left_curve_item, right_curve)]
            return matching

        def match_right_left(left_curve, right_curve, left_right_matching):
            matched_right_points = list(
                itertools.chain.from_iterable(left_right_matching.values())
            )
            unmatched_right_points = filter(
                lambda x: x not in matched_right_points,
                range(len(right_curve)),
            )
            updated_matching = deepcopy(left_right_matching)

            for right_point in unmatched_right_points:
                left_point = find_nearest_pair(
                    right_curve[right_point], left_curve
                )
                updated_matching[left_point].append(right_point)

            for key, value in updated_matching.items():
                updated_matching[key] = sorted(value)

            return updated_matching

        def reduce_interpolation(
            interpolated_points, matching, left_points, right_points
        ):
            def average_point(points):
                sumX = 0
                sumY = 0
                for point in points:
                    sumX += point["x"]
                    sumY += point["y"]

                return {"x": sumX / len(points), "y": sumY / len(points)}

            def compute_distance(point1, point2):
                return np.sqrt(
                    ((point1["x"] - point2["x"])) ** 2
                    + ((point1["y"] - point2["y"]) ** 2)
                )

            def minimize_segment(
                base_length, N, start_interpolated, stop_interpolated
            ):
                threshold = base_length / (2 * N)
                minimized = [interpolated_points[start_interpolated]]
                latest_pushed = start_interpolated
                for i in range(start_interpolated + 1, stop_interpolated):
                    distance = compute_distance(
                        interpolated_points[latest_pushed],
                        interpolated_points[i],
                    )

                    if distance >= threshold:
                        minimized.append(interpolated_points[i])
                        latest_pushed = i

                minimized.append(interpolated_points[stop_interpolated])

                if len(minimized) == 2:
                    distance = compute_distance(
                        interpolated_points[start_interpolated],
                        interpolated_points[stop_interpolated],
                    )

                    if distance < threshold:
                        return [average_point(minimized)]

                return minimized

            reduced = []
            interpolated_indexes = {}
            accumulated = 0
            for i in range(len(left_points)):
                interpolated_indexes[i] = []
                for _ in range(len(matching[i])):
                    interpolated_indexes[i].append(accumulated)
                    accumulated += 1

            def left_segment(start, stop):
                start_interpolated = interpolated_indexes[start][0]
                stop_interpolated = interpolated_indexes[stop][0]

                if start_interpolated == stop_interpolated:
                    reduced.append(interpolated_points[start_interpolated])
                    return

                base_length = curve_length(left_points[start : stop + 1])
                N = stop - start + 1

                reduced.extend(
                    minimize_segment(
                        base_length, N, start_interpolated, stop_interpolated
                    )
                )

            def right_segment(left_point):
                start = matching[left_point][0]
                stop = matching[left_point][-1]
                start_interpolated = interpolated_indexes[left_point][0]
                stop_interpolated = interpolated_indexes[left_point][-1]
                base_length = curve_length(right_points[start : stop + 1])
                N = stop - start + 1

                reduced.extend(
                    minimize_segment(
                        base_length, N, start_interpolated, stop_interpolated
                    )
                )

            previous_opened = None
            for i in range(len(left_points)):
                if len(matching[i]) == 1:
                    if previous_opened is not None:
                        if matching[i][0] == matching[previous_opened][0]:
                            continue
                        else:
                            start = previous_opened
                            stop = i - 1
                            left_segment(start, stop)
                            previous_opened = i
                    else:
                        previous_opened = i
                else:
                    if previous_opened is not None:
                        start = previous_opened
                        stop = i - 1
                        left_segment(start, stop)
                        previous_opened = None

                    right_segment(i)

            if previous_opened is not None:
                left_segment(previous_opened, len(left_points) - 1)

            return reduced

        left_points = to_points(left_position["points"])
        right_points = to_points(right_position["points"])
        left_offset_vec = curve_to_offset_vec(
            left_points, curve_length(left_points)
        )
        right_offset_vec = curve_to_offset_vec(
            right_points, curve_length(right_points)
        )

        matching = match_left_right(left_offset_vec, right_offset_vec)
        completed_matching = match_right_left(
            left_offset_vec, right_offset_vec, matching
        )

        interpolated_points = []
        for left_point_index, left_point in enumerate(left_points):
            for right_point_index in completed_matching[left_point_index]:
                right_point = right_points[right_point_index]
                interpolated_points.append(
                    {
                        "x": left_point["x"]
                        + (right_point["x"] - left_point["x"]) * offset,
                        "y": left_point["y"]
                        + (right_point["y"] - left_point["y"]) * offset,
                    }
                )

        reducedPoints = reduce_interpolation(
            interpolated_points, completed_matching, left_points, right_points
        )

        return to_array(reducedPoints).tolist()

    def polyshape_interpolation(shape0, shape1):
        shapes = []
        is_polygon = shape0["type"] == "polygon"
        if is_polygon:
            shape0["points"].extend(shape0["points"][:2])
            shape1["points"].extend(shape1["points"][:2])

        distance = shape1["frame"] - shape0["frame"]
        for frame in range(shape0["frame"] + 1, shape1["frame"]):
            offset = (frame - shape0["frame"]) / distance
            points = interpolate_position(shape0, shape1, offset)

            shapes.append(copy_shape(shape0, frame, points))

        if is_polygon:
            shape0["points"] = shape0["points"][:-2]
            shape1["points"] = shape1["points"][:-2]
            for shape in shapes:
                shape["points"] = shape["points"][:-2]

        return shapes

    def interpolate(shape0, shape1):
        is_same_type = shape0["type"] == shape1["type"]
        is_rectangle = shape0["type"] == "rectangle"
        is_cuboid = shape0["type"] == "cuboid"
        is_polygon = shape0["type"] == "polygon"
        is_polyline = shape0["type"] == "polyline"
        is_points = shape0["type"] == "points"

        if not is_same_type:
            raise NotImplementedError()

        shapes = []
        if is_rectangle or is_cuboid:
            shapes = simple_interpolation(shape0, shape1)
        elif is_points:
            shapes = points_interpolation(shape0, shape1)
        elif is_polygon or is_polyline:
            shapes = polyshape_interpolation(shape0, shape1)
        else:
            raise NotImplementedError()

        return shapes

    if not track_shapes:
        return []

    if len(track_shapes) == 1:
        track_shapes[0]["keyframe"] = True
        return track_shapes

    shapes = []
    curr_frame = track_shapes[0]["frame"]
    end_frame = track_shapes[-1]["frame"]
    prev_shape = {}
    for shape in track_shapes:
        if prev_shape:
            if shape["frame"] <= curr_frame:
                continue

            for attr in prev_shape["attributes"]:
                if attr["spec_id"] not in map(
                    lambda el: el["spec_id"], shape["attributes"]
                ):
                    shape["attributes"].append(deepcopy(attr))

            if not prev_shape["outside"]:
                shapes.extend(interpolate(prev_shape, shape))

        shape["keyframe"] = True
        shapes.append(shape)

        curr_frame = shape["frame"]
        prev_shape = shape

        if end_frame <= curr_frame:
            break

    if not prev_shape["outside"]:
        shape = deepcopy(prev_shape)
        shape["frame"] = end_frame
        shapes.extend(interpolate(prev_shape, shape))

    return shapes
