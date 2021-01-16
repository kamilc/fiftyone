"""
FiftyOne utilities unit tests.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import unittest

from mongoengine.errors import ValidationError
import numpy as np

import fiftyone as fo
import fiftyone.core.media as fom
from fiftyone.migrations.runner import Runner

from decorators import drop_datasets


class LabelsTests(unittest.TestCase):
    @drop_datasets
    def test_create(self):
        labels = fo.Classification(label="cow", confidence=0.98)
        self.assertIsInstance(labels, fo.Classification)

        with self.assertRaises(ValidationError):
            fo.Classification(label=100)

    @drop_datasets
    def test_copy(self):
        dataset = fo.Dataset()

        dataset.add_sample(
            fo.Sample(
                filepath="filepath1.jpg",
                test_dets=fo.Detections(
                    detections=[
                        fo.Detection(
                            label="friend",
                            confidence=0.9,
                            bounding_box=[0, 0, 0.5, 0.5],
                        )
                    ]
                ),
            )
        )

        sample = dataset.first()
        sample2 = sample.copy()

        self.assertIsNot(sample2, sample)
        self.assertNotEqual(sample2.id, sample.id)
        self.assertIsNot(sample2.test_dets, sample.test_dets)
        det = sample.test_dets.detections[0]
        det2 = sample2.test_dets.detections[0]
        self.assertIsNot(det2, det)
        self.assertNotEqual(det2.id, det.id)


class SerializationTests(unittest.TestCase):
    def test_embedded_document(self):
        label1 = fo.Classification(label="cat", logits=np.arange(4))

        label2 = fo.Classification(label="cat", logits=np.arange(4))

        d1 = label1.to_dict()
        d2 = label2.to_dict()
        d1.pop("_id")
        d2.pop("_id")
        self.assertDictEqual(d1, d2)

        d = label1.to_dict()
        self.assertEqual(fo.Classification.from_dict(d), label1)

        s = label1.to_json(pretty_print=False)
        self.assertEqual(fo.Classification.from_json(s), label1)

        s = label1.to_json(pretty_print=True)
        self.assertEqual(fo.Classification.from_json(s), label1)

    def test_sample_no_dataset(self):
        """This test only works if the samples do not have Classification or
        Detection fields because of the autogenerated ObjectIDs.
        """
        sample1 = fo.Sample(
            filepath="~/Desktop/test.png",
            tags=["test"],
            vector=np.arange(5),
            array=np.ones((2, 3)),
            float=5.1,
            bool=True,
            int=51,
        )

        sample2 = fo.Sample(
            filepath="~/Desktop/test.png",
            tags=["test"],
            vector=np.arange(5),
            array=np.ones((2, 3)),
            float=5.1,
            bool=True,
            int=51,
        )
        self.assertDictEqual(sample1.to_dict(), sample2.to_dict())

        self.assertEqual(
            fo.Sample.from_dict(sample1.to_dict()).to_dict(), sample1.to_dict()
        )

    @drop_datasets
    def test_sample_in_dataset(self):
        """This test only works if the samples do not have Classification or
        Detection fields because of the autogenerated ObjectIDs.
        """
        dataset1 = fo.Dataset()
        dataset2 = fo.Dataset()

        sample1 = fo.Sample(
            filepath="~/Desktop/test.png",
            tags=["test"],
            vector=np.arange(5),
            array=np.ones((2, 3)),
            float=5.1,
            bool=True,
            int=51,
        )

        sample2 = fo.Sample(
            filepath="~/Desktop/test.png",
            tags=["test"],
            vector=np.arange(5),
            array=np.ones((2, 3)),
            float=5.1,
            bool=True,
            int=51,
        )

        self.assertDictEqual(sample1.to_dict(), sample2.to_dict())

        dataset1.add_sample(sample1)
        dataset2.add_sample(sample2)

        self.assertNotEqual(sample1, sample2)

        s1 = fo.Sample.from_dict(sample1.to_dict())
        s2 = fo.Sample.from_dict(sample2.to_dict())

        self.assertFalse(s1.in_dataset)
        self.assertNotEqual(s1, sample1)

        self.assertDictEqual(s1.to_dict(), s2.to_dict())


class MediaTypeTests(unittest.TestCase):
    @drop_datasets
    def setUp(self):
        self.img_sample = fo.Sample(filepath="image.png")
        self.img_dataset = fo.Dataset()
        self.img_dataset.add_sample(self.img_sample)

        self.vid_sample = fo.Sample(filepath="video.mp4")
        self.vid_dataset = fo.Dataset()
        self.vid_dataset.add_sample(self.vid_sample)

    def test_img_types(self):
        self.assertEqual(self.img_sample.media_type, fom.IMAGE)
        self.assertEqual(self.img_dataset.media_type, fom.IMAGE)

    def test_vid_types(self):
        self.assertEqual(self.vid_sample.media_type, fom.VIDEO)
        self.assertEqual(self.vid_dataset.media_type, fom.VIDEO)

    def test_img_change_attempts(self):
        with self.assertRaises(fom.MediaTypeError):
            self.img_sample.filepath = "video.mp4"

    def test_vid_change_attempts(self):
        with self.assertRaises(fom.MediaTypeError):
            self.vid_sample.filepath = "image.png"


class MigrationTests(unittest.TestCase):
    def test_runner(self):
        def revs(versions):
            return list(map(lambda v: (v, v + ".py"), versions))

        runner = Runner(
            head=None, destination="0.3", revisions=revs(["0.1", "0.2", "0.3"])
        )
        self.assertEqual(runner.revisions, ["0.1", "0.2", "0.3"])
        runner = Runner(
            head="0.1",
            destination="0.3",
            revisions=revs(["0.1", "0.2", "0.3"]),
        )
        self.assertEqual(runner.revisions, ["0.2", "0.3"])
        runner = Runner(
            head="0.3", destination=None, revisions=revs(["0.1", "0.2", "0.3"])
        )
        self.assertEqual(runner.revisions, ["0.3", "0.2", "0.1"])
        runner = Runner(head=None, destination="0.1", revisions=revs(["0.1"]))
        self.assertEqual(runner.revisions, ["0.1"])


if __name__ == "__main__":
    fo.config.show_progress_bars = False
    unittest.main(verbosity=2)
