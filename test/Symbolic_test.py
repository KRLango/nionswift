# futures
from __future__ import absolute_import

# standard libraries
import logging
import random
import unittest
import uuid

# third party libraries
import numpy
import scipy

# local libraries
from nion.swift import Application
from nion.swift import DocumentController
from nion.swift.model import DataItem
from nion.swift.model import DocumentModel
from nion.swift.model import Region
from nion.swift.model import Symbolic
from nion.ui import Test


class TestSymbolicClass(unittest.TestCase):

    def setUp(self):
        self.app = Application.Application(Test.UserInterface(), set_global=False)

    def tearDown(self):
        pass

    def test_unary_inversion_returns_inverted_data(self):
        d = numpy.zeros((8, 8), dtype=numpy.uint32)
        d[:] = random.randint(0, 100)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("-a", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, -d)

    def test_binary_addition_returns_added_data(self):
        d1 = numpy.zeros((8, 8), dtype=numpy.uint32)
        d1[:] = random.randint(0, 100)
        data_item1 = DataItem.DataItem(d1)
        d2 = numpy.zeros((8, 8), dtype=numpy.uint32)
        d2[:] = random.randint(0, 100)
        data_item2 = DataItem.DataItem(d2)
        map = {"a": {"type": "data_item", "uuid": str(data_item1.uuid)}, "b": {"type": "data_item", "uuid": str(data_item2.uuid)}}
        reverse_map = {data_item1.uuid: data_item1.maybe_data_source.data_and_calibration, data_item2.uuid: data_item2.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("a+b", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, d1 + d2)

    def test_binary_multiplication_with_scalar_returns_multiplied_data(self):
        d = numpy.zeros((8, 8), dtype=numpy.uint32)
        d[:] = random.randint(0, 100)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node1, mapping1 = Symbolic.parse_expression("a * 5", map)
        def resolve1(uuid_):
            return reverse_map[uuid.UUID(mapping1[uuid_]["uuid"])]
        data1 = data_node1.evaluate(resolve1).data
        data_node2, mapping2 = Symbolic.parse_expression("5 * a", map)
        def resolve2(uuid_):
            return reverse_map[uuid.UUID(mapping2[uuid_]["uuid"])]
        data2 = data_node2.evaluate(resolve2).data
        assert numpy.array_equal(data1, d * 5)
        assert numpy.array_equal(data2, d * 5)

    def test_subtract_min_returns_subtracted_min(self):
        d = numpy.zeros((8, 8), dtype=numpy.uint32)
        d[:] = random.randint(0, 100)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("a - min(a)", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, d - numpy.amin(d))

    def test_ability_to_take_slice(self):
        d = numpy.zeros((4, 8, 8), dtype=numpy.uint32)
        d[:] = random.randint(0, 100)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("a[:,4,4]", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, d[:,4,4])

    def test_ability_to_write_read_basic_nodes(self):
        d = numpy.zeros((8, 8), dtype=numpy.uint32)
        d[:] = random.randint(0, 100)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("-a / average(a) * 5", map)
        data_node_dict = data_node.write()
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data_node2 = Symbolic.DataNode.factory(data_node_dict)
        data = data_node.evaluate(resolve).data
        data2 = data_node2.evaluate(resolve).data
        assert numpy.array_equal(data, -d / numpy.average(d) * 5)
        assert numpy.array_equal(data, data2)

    def test_make_operation_works_without_exception_and_produces_correct_data(self):
        document_model = DocumentModel.DocumentModel()
        document_controller = DocumentController.DocumentController(self.app.ui, document_model, workspace_id="library")
        d = numpy.zeros((8, 8), dtype=numpy.uint32)
        d[:] = random.randint(0, 100)
        data_item = DataItem.DataItem(d)
        document_model.append_data_item(data_item)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        data_item = document_controller.processing_calculation("-a / average(a) * 5", map)
        document_model.recompute_all()
        assert numpy.array_equal(data_item.maybe_data_source.data, -d / numpy.average(d) * 5)

    def test_fft_returns_complex_data(self):
        d = numpy.random.randn(64, 64)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("fft(a)", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, scipy.fftpack.fftshift(scipy.fftpack.fft2(d) * 1.0 / numpy.sqrt(d.shape[1] * d.shape[0])))

    def test_gaussian_blur_handles_scalar_argument(self):
        d = numpy.random.randn(64, 64)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("gaussian_blur(a, 4.0)", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, scipy.ndimage.gaussian_filter(d, sigma=4.0))

    def test_transpose_flip_handles_args(self):
        d = numpy.random.randn(30, 60)
        data_item = DataItem.DataItem(d)
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration}
        data_node, mapping = Symbolic.parse_expression("transpose_flip(a, flip_v=True)", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, numpy.flipud(d))

    def test_crop_handles_args(self):
        d = numpy.random.randn(64, 64)
        data_item = DataItem.DataItem(d)
        region = Region.RectRegion()
        region.center = 0.41, 0.51
        region.size = 0.52, 0.42
        map = {"a": {"type": "data_item", "uuid": str(data_item.uuid)}, "regionA": {"type": "region", "uuid": str(region.uuid)}}
        reverse_map = {data_item.uuid: data_item.maybe_data_source.data_and_calibration, region.uuid: region}
        data_node, mapping = Symbolic.parse_expression("crop(a, regionA.bounds)", map)
        def resolve(uuid_):
            return reverse_map[uuid.UUID(mapping[uuid_]["uuid"])]
        data = data_node.evaluate(resolve).data
        assert numpy.array_equal(data, d[9:42, 19:45])

    def test_evaluate_computation_within_document_model_gives_correct_value(self):
        document_model = DocumentModel.DocumentModel()
        data = numpy.ones((2, 2), numpy.double)
        data_item = DataItem.DataItem(data)
        document_model.append_data_item(data_item)
        computation = Symbolic.Computation()
        map = {"a": document_model.get_object_specifier(data_item)}
        computation.parse_expression(document_model, "-a", map)
        data_and_metadata = computation.evaluate()
        self.assertTrue(numpy.array_equal(data_and_metadata.data, -data))


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.DEBUG)
    unittest.main()
