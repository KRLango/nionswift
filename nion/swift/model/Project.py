# standard libraries
import copy
import logging
import pathlib
import typing
import uuid

# local libraries
from nion.swift.model import DataItem
from nion.swift.model import FileStorageSystem
from nion.utils import Event
from nion.utils import Observable
from nion.utils import Persistence


class Project(Observable.Observable, Persistence.PersistentObject):
    """A project manages raw data items, display items, computations, data structures, and connections.

    Projects are stored in project indexes, which are files that describe how to find data and and tracks the other
    project relationships (display items, computations, data structures, connections).

    Projects manage reading, writing, and data migration.
    """

    PROJECT_VERSION = 3

    def __init__(self, storage_system: FileStorageSystem.ProjectStorageSystem, project_reference: typing.Dict):
        super().__init__()

        self.define_type("project")
        self.define_relationship("data_items", data_item_factory, insert=self.__data_item_inserted, remove=self.__data_item_removed)

        self.__project_reference = copy.deepcopy(project_reference)
        self.__project_state = None
        self.__project_version = 0

        self._raw_properties = None  # debugging

        self.__storage_system = storage_system

        self.persistent_storage = self.__storage_system

        self.item_loaded_event = Event.Event()
        self.item_unloaded_event = Event.Event()

    def open(self) -> None:
        self.__storage_system.reset()  # this makes storage reusable during tests

    def close(self):
        for data_item in self.data_items:
            data_item.about_to_close()
        for data_item in self.data_items:
            data_item.about_to_be_removed()
        for data_item in self.data_items:
            data_item.close()

    @property
    def needs_upgrade(self) -> bool:
        return self.__project_reference.get("type") == "project_folder"

    @property
    def project_reference(self) -> typing.Dict:
        return copy.deepcopy(self.__project_reference)

    @property
    def project_reference_parts(self) -> typing.Tuple[str]:
        if self.__project_reference.get("type") == "project_folder":
            return pathlib.Path(self.__storage_system.get_identifier()).parent.parts
        else:
            return pathlib.Path(self.__storage_system.get_identifier()).parts

    @property
    def project_state(self) -> str:
        return self.__project_state

    @property
    def project_version(self) -> int:
        return self.__project_version

    @property
    def project_storage_system(self) -> FileStorageSystem.ProjectStorageSystem:
        return self.__storage_system

    def persistent_object_context_changed(self):
        super().persistent_object_context_changed()
        self.persistent_object_context._set_persistent_storage_for_object(self, self.__storage_system)

    def __handle_load_data_item(self, item_d: typing.Dict, item_uuid: uuid.UUID = None, large_format: bool = False) -> typing.Optional[DataItem.DataItem]:
        data_item = DataItem.DataItem(item_uuid=item_uuid, large_format=large_format)
        data_item.begin_reading()
        data_item.read_from_dict(item_d)
        data_item.finish_reading()
        if data_item.uuid not in {data_item.uuid for data_item in self.data_items}:
            self.persistent_object_context._set_persistent_storage_for_object(data_item, self.__storage_system)
            before_index = len(self.data_items)
            self.load_item("data_items", before_index, data_item)
            return data_item
        return None

    def __data_item_inserted(self, name: str, before_index: int, data_item: DataItem.DataItem) -> None:
        data_item.about_to_be_inserted(self)
        self.notify_insert_item("data_items", data_item, before_index)

    def __data_item_removed(self, name: str, index: int, data_item: DataItem.DataItem) -> None:
        data_item.about_to_be_removed()
        self.notify_remove_item("data_items", data_item, index)
        data_item.close()

    def read_project(self) -> None:
        # first read the library (for deletions) and the library items from the primary storage systems
        logging.getLogger("loader").info(f"Loading project {self.__storage_system.get_identifier()}")
        properties = self.__storage_system.read_project_properties()  # combines library and data item properties
        self.__project_version = properties.get("version", 0)
        if self.__project_version in (FileStorageSystem.PROJECT_VERSION, 2):
            for item_type in ("data_items", ):
                for item_d in properties.get(item_type, list()):
                    self.__handle_load_data_item(item_d)
            for item_type in ("display_items", "data_structures", "connections", "computations"):
                for item_d in properties.get(item_type, list()):
                    self.item_loaded_event.fire(item_type, item_d, self.__storage_system)
            self.__project_state = "loaded"
        else:
            self.__project_state = "needs_upgrade"
        self._raw_properties = properties

    def append_data_item(self, data_item: DataItem.DataItem) -> None:
        assert data_item.uuid not in {data_item.uuid for data_item in self.data_items}
        self.persistent_object_context._set_persistent_storage_for_object(data_item, self.__storage_system)
        self.append_item("data_items", data_item)
        # don't directly write data item, or else write_pending is not cleared on data item
        # call finish pending write instead
        data_item._finish_pending_write()  # initially write to disk

    def remove_data_item(self, data_item: DataItem.DataItem) -> None:
        self.remove_item("data_items", data_item)

    def restore_data_item(self, data_item_uuid: uuid.UUID) -> typing.Optional[DataItem.DataItem]:
        item_d = self.__storage_system.restore_item(data_item_uuid)
        if item_d is not None:
            data_item_uuid = uuid.UUID(item_d.get("uuid"))
            large_format = item_d.get("__large_format", False)
            data_item = DataItem.DataItem(item_uuid=data_item_uuid, large_format=large_format)
            data_item.begin_reading()
            data_item.read_from_dict(item_d)
            data_item.finish_reading()
            assert data_item.uuid not in {data_item.uuid for data_item in self.data_items}
            self.persistent_object_context._set_persistent_storage_for_object(data_item, self.__storage_system)
            self.append_item("data_items", data_item)
            return data_item
        return None

    def prune(self) -> None:
        self.__storage_system.prune()

    def migrate_to_latest(self) -> None:
        self.__storage_system.migrate_to_latest()
        self.__storage_system.load_properties()
        self.read_project()


def data_item_factory(lookup_id):
    data_item_uuid = uuid.UUID(lookup_id("uuid"))
    large_format = lookup_id("__large_format", False)
    return DataItem.DataItem(item_uuid=data_item_uuid, large_format=large_format)


def make_project(profile_context, project_reference: typing.Dict) -> typing.Optional[Project]:
    project_storage_system = FileStorageSystem.make_storage_system(profile_context, project_reference)
    project_storage_system.load_properties()
    if project_storage_system:
        return Project(project_storage_system, project_reference)
    return None