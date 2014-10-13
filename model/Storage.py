# standard libraries
import collections
import copy
import cPickle as pickle
import functools
import logging
import json
import os
import Queue
import sqlite3
import StringIO
import threading
import time
import uuid
import weakref

# third party libraries
import numpy

# local libraries
from nion.ui import Observable


class MutableRelationship(collections.MutableSequence):

    """
        An observable list that integrates into the storage system.

        Items in the list are recursively observed.
    """

    def __init__(self, parent, relationship_name):
        """
            parent is notified when items are inserted or removed.
            relationship_name is passed to parent when notifying.
        """
        self.__list = list()
        self.relationship_name = relationship_name
        self.parent_weak_ref = weakref.ref(parent)

    def __copy__(self):
        return copy.copy(self.__list)

    def __len__(self):
        return len(self.__list)

    def __getitem__(self, index):
        return self.__list[index]

    def __setitem__(self, index, value):
        raise IndexError()

    def __delitem__(self, index):
        # get value
        value = self.__list[index]
        # unobserve
        value.remove_observer(self.parent_weak_ref())
        # do actual removal
        del self.__list[index]
        # keep storage up-to-date
        self.parent_weak_ref().notify_remove_item(self.relationship_name, value, index)

    def __iter__(self):
        return iter(self.__list)

    def insert(self, index, value):
        assert value not in self.__list
        assert index <= len(self.__list) and index >= 0
        # insert in internal list
        self.__list.insert(index, value)
        # observe
        value.add_observer(self.parent_weak_ref())
        # keep storage up-to-date
        self.parent_weak_ref().notify_insert_item(self.relationship_name, value, index)


class Cacheable(object):

    def __init__(self):
        super(Cacheable, self).__init__()
        self.__storage_cache = None
        self.__cache = dict()
        self.__cache_dirty = dict()
        self.__cache_mutex = threading.RLock()

    def get_storage_cache(self):
        return self.__storage_cache
    def set_storage_cache(self, storage_cache):
        self.__storage_cache = storage_cache
        self.storage_cache_changed(storage_cache)
        self.spill_cache()
    storage_cache = property(get_storage_cache, set_storage_cache)

    def storage_cache_changed(self, storage_cache):
        pass

    def _is_cache_delayed(self):
        return False

    # the cache system stores values that are expensive to calculate for quick retrieval.
    # an item can be marked dirty in the cache so that callers can determine whether that
    # value needs to be recalculated. marking a value as dirty doesn't affect the current
    # value in the cache. callers can still retrieve the latest value for an item in the
    # cache even when it is marked dirty. this way the cache is used to retrieve the best
    # available data without doing additional calculations.

    # move local cache items into permanent cache when transaction is finished.
    def spill_cache(self):
        with self.__cache_mutex:
            cache_copy = copy.copy(self.__cache)
            cache_dirty_copy = copy.copy(self.__cache_dirty)
            self.__cache.clear()
            self.__cache_dirty.clear()
        for key, value in cache_copy.iteritems():
            if self.storage_cache:
                self.storage_cache.set_cached_value(self, key, value, cache_dirty_copy.get(key, False))

    # update the value in the cache. usually updating a value in the cache
    # means it will no longer be dirty.
    def set_cached_value(self, key, value, dirty=False):
        # if transaction count is 0, cache directly
        if self.storage_cache and not self._is_cache_delayed():
            self.storage_cache.set_cached_value(self, key, value, dirty)
        # otherwise, store it temporarily until transaction is finished
        else:
            with self.__cache_mutex:
                self.__cache[key] = value
                self.__cache_dirty[key] = dirty

    # grab the last cached value, if any, from the cache.
    def get_cached_value(self, key, default_value=None):
        # first check temporary cache.
        with self.__cache_mutex:
            if key in self.__cache:
                return self.__cache.get(key)
        # not there, go to cache db
        if self.storage_cache:
            return self.storage_cache.get_cached_value(self, key, default_value)
        return default_value

    # removing values from the cache happens immediately under a transaction.
    # this is an area of improvement if it becomes a bottleneck.
    def remove_cached_value(self, key):
        # remove it from the cache db.
        if self.storage_cache:
            self.storage_cache.remove_cached_value(self, key)
        # if its in the temporary cache, remove it
        with self.__cache_mutex:
            if key in self.__cache:
                del self.__cache[key]
            if key in self.__cache_dirty:
                del self.__cache_dirty[key]

    # determines whether the item in the cache is dirty.
    def is_cached_value_dirty(self, key):
        # check the temporary cache first
        with self.__cache_mutex:
            if key in self.__cache_dirty:
                return self.__cache_dirty[key]
        # not there, go to the db cache
        if self.storage_cache:
            return self.storage_cache.is_cached_value_dirty(self, key)
        return True

    # set whether the cache value is dirty.
    def set_cached_value_dirty(self, key, dirty=True):
        # go directory to the db cache if not under a transaction
        if self.storage_cache and not self._is_cache_delayed():
            self.storage_cache.set_cached_value_dirty(self, key, dirty)
        # otherwise mark it in the temporary cache
        else:
            with self.__cache_mutex:
                self.__cache_dirty[key] = dirty


#
# StorageBase supports observers and listeners.
#
# Observers can watch all serializable changes to the object by
# adding themselves as an observer and then overriding one or more
# of the following methods:
#   property_changed(object, key, value)
#   item_set(object, key, value)
#   item_cleared(object, key)
#   data_set(object, key, data)
#   item_inserted(object, key, value, before_index)
#   item_removed(object, key, value, index)
#
# Listeners listen to any notifications broadcast. They
# take the form of specific method calls on the listeners.
#
# Connections are automatically controlled listeners. They
# will be removed when the reference count goes to zero.
#

class StorageBase(Observable.Observable, Observable.Broadcaster, Cacheable):

    def __init__(self):
        super(StorageBase, self).__init__()
        self.__datastore = None
        self.storage_properties = []
        self.storage_relationships = []
        self.storage_items = []
        self.storage_type = None
        self.__reverse_aliases = dict()
        self.__weak_parents = []
        self.__uuid = uuid.uuid4()

    # uuid property. read only.
    def __get_uuid(self):
        return self.__uuid
    uuid = property(__get_uuid)
    # set is used by document controller
    def _set_uuid(self, uuid):
        self.__uuid = uuid

    # Add a parent.
    def add_parent(self, parent):
        assert parent is not None
        self.__weak_parents.append(weakref.ref(parent))

    # Remove a parent.
    def remove_parent(self, parent):
        assert parent is not None
        self.__weak_parents.remove(weakref.ref(parent))

    # Return a copy of parents array
    def get_weak_parents(self):
        return self.__weak_parents  # TODO: Return a copy
    def __get_parents(self):
        return [weak_parent() for weak_parent in self.__weak_parents]
    parents = property(__get_parents)

    # map a property to a storage key
    def register_key_alias(self, key, alias):
        self.__reverse_aliases[key] = alias

    # Send a message to the parents
    def notify_parents(self, fn, *args, **keywords):
        for parent in self.parents:
            if hasattr(parent, fn):
                getattr(parent, fn)(*args, **keywords)

    def __get_datastore(self):
        return self.__datastore
    def __set_datastore(self, datastore):
        self.__datastore = datastore
        for item_key in self.storage_items:
            item = self.get_storage_item(item_key)
            if item:
                item.datastore = datastore
        for relationship_key in self.storage_relationships:
            count = self.get_storage_relationship_count(relationship_key)
            for index in range(count):
                item = self.get_storage_relationship_item(relationship_key, index)
                item.datastore = datastore
    datastore = property(__get_datastore, __set_datastore)

    def set_storage_cache(self, storage_cache):
        for item_key in self.storage_items:
            item = self.get_storage_item(item_key)
            if item:
                item.storage_cache = storage_cache
        for relationship_key in self.storage_relationships:
            count = self.get_storage_relationship_count(relationship_key)
            for index in range(count):
                item = self.get_storage_relationship_item(relationship_key, index)
                item.storage_cache = storage_cache
        super(StorageBase, self).set_storage_cache(storage_cache)

    def get_storage_property(self, key):
        if hasattr(self, key):
            return getattr(self, key)
        if hasattr(self, "get_" + key):
            return getattr(self, "get_" + key)()
        if hasattr(self, "_get_" + key):
            return getattr(self, "_get_" + key)()
        logging.debug("get_storage_property: %s missing %s", self, key)
        raise NotImplementedError()

    def get_storage_item(self, key):
        if hasattr(self, key):
            return getattr(self, key)
        if hasattr(self, "get_" + key):
            return getattr(self, "get_" + key)()
        if hasattr(self, "_get_" + key):
            return getattr(self, "_get_" + key)()
        logging.debug("get_storage_item: %s missing %s", self, key)
        raise NotImplementedError()

    def get_storage_relationship_count(self, key):
        relationship = self.get_storage_relationship(key)
        if relationship is not None:
            return len(relationship)
        logging.debug("get_storage_relationship_count: %s missing %s", self, key)
        raise NotImplementedError()

    def get_storage_relationship_item(self, key, index):
        relationship = self.get_storage_relationship(key)
        if relationship is not None:
            return relationship[index]
        logging.debug("get_storage_relationship_item: %s missing %s[%d]", self, key, index)
        raise NotImplementedError()

    def get_storage_relationship(self, key):
        if hasattr(self, key):
            return getattr(self, key)
        if hasattr(self, "get_" + key):
            return getattr(self, "get_" + key)()
        if hasattr(self, "_get_" + key):
            return getattr(self, "_get_" + key)()
        return None

    # implement observer/notification mechanism

    def notify_set_property(self, key, value):
        if key in self.storage_properties:
            if self.datastore:
                resolved_key = self.__reverse_aliases.get(key, key)
                self.datastore.set_property(self, resolved_key, value)
        super(StorageBase, self).notify_set_property(key, value)

    def notify_set_item(self, key, item):
        if key in self.storage_items:
            assert item is not None
            if self.datastore:
                item.datastore = self.datastore
                resolved_key = self.__reverse_aliases.get(key, key)
                self.datastore.set_item(self, resolved_key, item)
            if self.storage_cache:
                item.storage_cache = self.storage_cache
            if item:
                item.add_parent(self)
        super(StorageBase, self).notify_set_item(key, item)

    def notify_clear_item(self, key):
        if key in self.storage_items:
            item = self.get_storage_item(key)
            if item:
                if self.datastore:
                    resolved_key = self.__reverse_aliases.get(key, key)
                    self.datastore.clear_item(self, resolved_key)
                    item.datastore = None
                if self.storage_cache:
                    item.storage_cache = None
                item.remove_parent(self)
        super(StorageBase, self).notify_clear_item(key)

    def notify_insert_item(self, key, value, before_index):
        if key in self.storage_relationships:
            assert value is not None
            if self.datastore:
                value.datastore = self.datastore
                resolved_key = self.__reverse_aliases.get(key, key)
                self.datastore.insert_item(self, resolved_key, value, before_index)
            if self.storage_cache:
                value.storage_cache = self.storage_cache
            value.add_parent(self)
        super(StorageBase, self).notify_insert_item(key, value, before_index)

    def notify_remove_item(self, key, value, index):
        if key in self.storage_relationships:
            assert value is not None
            if self.datastore:
                resolved_key = self.__reverse_aliases.get(key, key)
                self.datastore.remove_item(self, resolved_key, index)
                value.datastore = None
            if self.storage_cache:
                value.storage_cache = None
            value.remove_parent(self)
        super(StorageBase, self).notify_remove_item(key, value, index)

    def write(self):
        assert self.datastore is not None
        for property_key in self.storage_properties:
            value = self.get_storage_property(property_key)
            if value:
                resolved_property_key = self.__reverse_aliases.get(property_key, property_key)
                self.datastore.set_property(self, property_key, value)
        for item_key in self.storage_items:
            item = self.get_storage_item(item_key)
            if item:
                # TODO: are these redundant?
                item.datastore = self.datastore
                item.storage_cache = self.storage_cache
                resolved_item_key = self.__reverse_aliases.get(item_key, item_key)
                self.datastore.set_item(self, resolved_item_key, item)
        for relationship_key in self.storage_relationships:
            count = self.get_storage_relationship_count(relationship_key)
            for index in range(count):
                item = self.get_storage_relationship_item(relationship_key, index)
                # TODO: are these redundant?
                item.datastore = self.datastore
                item.storage_cache = self.storage_cache
                resolved_relationship_key = self.__reverse_aliases.get(relationship_key, relationship_key)
                self.datastore.insert_item(self, resolved_relationship_key, item, index)
        if self.datastore:
            self.datastore.set_type(self, self.storage_type)

    # only used for testing
    def rewrite_object(self):
        assert self.datastore is not None
        self.datastore.erase_object(self)
        self.write()


# design considerations: fast, threaded, future proof, object oriented items
# two techniques:
# - model objects know about storage, explicitly manage storage
# - model objects have listener architecture, storage listens to each
# overall design can use a combination of the two techniques. the parent must
# act as a storage liason if the listener architecture is used.
# in addition to actively communicating changes to storage, the items must be
# able to create themselves from storage and be able to write themselves to
# storage.
# objects in storage must be able to provide type, uuid, constructor, write
# changes to the object model are only allowed on the main thread. this allows
# everything to be serialized properly.
# blob support
# db revision support
# save thumbnails?
# save processed data?
# TODO: revisit core data design from Apple
class DictDatastore(object):

    def __init__(self, node_map=None):
        self.__node_map = node_map if node_map else {}
        # item map is used during item construction.
        self.__item_map = {}
        self.__data_map = {}
        self.initialized = node_map is not None
        self.disconnected = False

    def __get_node_map(self):
        return self.__node_map
    node_map = property(__get_node_map)

    def set_root(self, root):
        if self.disconnected:
            return
        self.__make_node(root.uuid)
        self.initialized = True

    def log(self):
        for key in self.__node_map.keys():
            logging.debug("%s %s: %s", type(self.__node_map[key]), key, self.__node_map[key])

    def __make_node(self, uuid):
        if uuid in self.__node_map:
            return self.__node_map[uuid]
        else:
            node = {}
            node["ref-count"] = 0
            self.__node_map[uuid] = node
            return node

    def find_node_or_none(self, item):
        return self.__node_map[item.uuid] if item.uuid in self.__node_map else None

    def find_node(self, item):
        return self.__node_map[item.uuid]

    def __add_node_ref(self, uuid):
        self.__node_map[uuid]["ref-count"] += 1

    def __remove_node_ref(self, uuid_, ignore_refcount=False):
        node = self.__node_map[uuid_]
        if not ignore_refcount:
            node["ref-count"] -= 1
            refcount = node["ref-count"]
        if ignore_refcount or refcount == 0:
            # first remove the items
            items = node.get("items", {})
            for item_key in items.keys():
                item_uuid = items[item_key]
                self.__remove_node_ref(item_uuid)
                del items[item_key]
            # next remove the relationships
            relationships = node.get("relationships", {})
            for relationship_key in relationships.keys():
                list = relationships[relationship_key]
                for item_uuid in list:
                    self.__remove_node_ref(item_uuid)
                del relationships[relationship_key]
            # remove data
            self.__erase_data_reference(uuid_)
            # delete the node itself
            del self.__node_map[uuid_]

    def __erase_data_reference(self, uuid_):
        item_node = self.__node_map[uuid_]
        reference_type = item_node.get("reference_types", {}).get("master_data")
        reference = item_node.get("references", {}).get("master_data")
        if reference and reference in self.__data_map:
            del self.__data_map[reference]

    def erase_object(self, object):
        self.__remove_node_ref(object.uuid, True)

    def set_type(self, item, type):
        if self.disconnected:
            return
        # get the item node
        item_node = self.find_node(item)
        # write to it
        item_node["type"] = type

    def set_item(self, parent, key, item):
        if self.disconnected:
            return
        if not self.find_node_or_none(item):
            # make a node in storage
            node = self.__make_node(item.uuid)
            # write item to the new node
            item.write()
        # get the parent node
        parent_node = self.find_node(parent)
        # insert new node in parent
        items = parent_node.setdefault("items", {})
        items[key] = item.uuid
        self.__add_node_ref(item.uuid)

    def clear_item(self, parent, key):
        if self.disconnected:
            return
        # get the parent node
        parent_node = self.find_node(parent)
        # find the node we will remove
        items = parent_node["items"]
        item_uuid = items[key]
        del items[key]
        self.__remove_node_ref(item_uuid)

    def insert_item(self, parent, key, item, before):
        if self.disconnected:
            return
        if not self.find_node_or_none(item):
            # make a node in storage
            node = self.__make_node(item.uuid)
            # write item to the new node
            item.write()
        # get the parent node
        parent_node = self.find_node(parent)
        # insert new node in parent
        relationships = parent_node.setdefault("relationships", {})
        list = relationships.setdefault(key, [])
        list.insert(before, item.uuid)
        self.__add_node_ref(item.uuid)

    def remove_item(self, parent, key, index):
        if self.disconnected:
            return
        # get the parent node
        parent_node = self.find_node(parent)
        # find the node we will remove
        relationships = parent_node["relationships"]
        list = relationships[key]
        item_uuid = list[index]
        del list[index]
        self.__remove_node_ref(item_uuid)

    def set_property(self, item, key, value):
        if self.disconnected:
            return
        # get the item node
        item_node = self.find_node(item)
        # write to it
        properties = item_node.setdefault("properties", {})
        properties[key] = copy.deepcopy(value)

    def write_data_reference(self, data, reference_type, reference, file_datetime):
        pass

    def write_properties(self, properties, reference_type, reference, file_datetime):
        pass

    # NOTE: parent_nodes are dicts for this class

    def find_root_node(self, type):
        for item in self.__node_map:
            if self.__node_map[item]["type"] == type:
                return self.__node_map[item], item
        return None

    def find_parent_node(self, item):
        return self.__node_map[item.uuid]

    def build_item(self, uuid_, node):
        item = None
        if uuid_ not in self.__item_map:
            from nion.swift.model import DataGroup
            from nion.swift.model import DataItem
            build_map = {
                "data-group": DataGroup.DataGroup,
                "data-item": DataItem.DataItem,
            }
            type = node["type"]
            if type in build_map:
                item = build_map[type].build(self, node)
                item._set_uuid(uuid_)
            if item:
                self.__item_map[uuid_] = item
            else:
                logging.debug("Unable to build %s", type)
        else:
            item = self.__item_map[uuid_]
        return item

    def has_data(self, parent_node, key):
        return "data_arrays" in parent_node and key in parent_node["data_arrays"]

    def has_item(self, parent_node, key):
        return "items" in parent_node and key in parent_node["items"]

    def has_relationship(self, parent_node, key):
        return "relationships" in parent_node and key in parent_node["relationships"]

    def get_item(self, parent_node, key, default_value=None):
        items = parent_node.get("items", {})
        if key in items:
            uuid_ = items[key]
            node = self.__node_map[uuid_]
            return self.build_item(uuid_, node)
        else:
            return default_value

    def get_items(self, parent_node, key, builder=None):
        builder = builder if builder else self.build_item
        relationships = parent_node.get("relationships", {})
        if key in relationships:
            return [builder(uuid, self.__node_map[uuid]) for uuid in relationships[key]]
        else:
            return []

    def get_property(self, parent_node, key, default_value=None):
        properties = parent_node.get("properties", {})
        if key in properties:
            return properties[key]
        else:
            return default_value

    def get_data_reference(self, parent_node, key):
        return "embedded", parent_node

    def load_data_reference(self, key, reference_type, reference):
        return self.__data_map.get(reference)

    def get_data_shape_and_dtype(self, parent_node, key):
        data = self.__data_map.get(reference)
        if data is not None:
            return data.shape, data.dtype
        return None

    # alternate, direct interface to db

    def find_data_item_tuples(self):
        tuples = []
        for item_uuid in self.__node_map.keys():
            item_node = self.__node_map[item_uuid]
            if item_node["type"] == "data-item":
                properties = self.get_root_property(item_uuid, "properties")
                reference_type, reference = self.get_root_data_reference(item_uuid, "master_data")
                tuples.append((item_uuid, properties, reference_type, reference))
        return tuples

    def add_root_item_uuid(self, key, item_uuid):
        if self.disconnected:
            return
        # make a node in storage
        item_node = self.__make_node(item_uuid)
        item_node["type"] = key
        # add reference count
        self.__add_node_ref(item_uuid)

    def remove_root_item_uuid(self, key, item_uuid, reference_type, reference):
        if self.disconnected:
            return
        self.__remove_node_ref(item_uuid)

    def get_root_property(self, item_uuid, key):
        # get the item node
        item_node = self.__node_map[item_uuid]
        # return the property
        properties = item_node.get("properties", {})
        if key in properties:
            return properties[key]
        return None

    def set_root_properties(self, item_uuid, properties, reference, file_datetime):
        if self.disconnected:
            return
        # get the item node
        item_node = self.__node_map[item_uuid]
        # write to it
        item_properties = item_node.setdefault("properties", {})
        item_properties["properties"] = copy.deepcopy(properties)  # ugh

    def set_root_data(self, item_uuid, data, data_shape, data_dtype, reference, file_datetime):
        if self.disconnected:
            return
        if data is None:
            return
        # get the item node
        item_node = self.__node_map[item_uuid]
        # insert new node in parent
        key = "master_data"
        data_arrays = item_node.setdefault("data_arrays", {})
        data_arrays[key] = copy.deepcopy(data)
        item_node.setdefault("reference_types", {})[key] = "relative_file"
        item_node.setdefault("references", {})[key] = reference
        item_node.setdefault("data_shapes", {})[key] = data_shape
        item_node.setdefault("data_dtypes", {})[key] = data_dtype
        self.__data_map[reference] = copy.deepcopy(data)

    def get_root_data_reference(self, item_uuid, key):
        # get the item node
        item_node = self.__node_map[item_uuid]
        # return reference_type, reference
        return item_node.get("reference_types", {}).get(key), item_node.get("references", {}).get(key)



def db_make_directory_if_needed(directory_path):
    if os.path.exists(directory_path):
        if not os.path.isdir(directory_path):
            raise OSError("Path is not a directory:", directory_path)
    else:
        os.makedirs(directory_path)

# utility function for db migration
def db_write_data_reference(c, parent_uuid, key, data_shape, data_dtype, reference_type, reference):
    args = list()
    args.append(str(parent_uuid))
    args.append(key)
    args.append(sqlite3.Binary(pickle.dumps(data_shape, pickle.HIGHEST_PROTOCOL)))
    args.append(sqlite3.Binary(pickle.dumps(data_dtype, pickle.HIGHEST_PROTOCOL)))
    args.append(reference_type)
    args.append(reference)
    c.execute("INSERT OR REPLACE INTO data_references (uuid, key, shape, dtype, reference_type, reference) VALUES (?, ?, ?, ?, ?, ?)", args)

# utility function to read data from external file.
def db_get_data_reference(c, parent_uuid, key):
    assert parent_uuid
    c.execute("SELECT reference_type, reference FROM data_references WHERE uuid=? AND key=?", (str(parent_uuid), key))
    data_row = c.fetchone()
    if data_row:
        return data_row[0], data_row[1]
    return "invalid", None

# utility function to read data shape and dtype from external file.
def db_get_data_shape_and_dtype(c, parent_uuid, key):
    c.execute("SELECT shape, dtype FROM data_references WHERE uuid=? AND key=?", (str(parent_uuid), key))
    data_row = c.fetchone()
    if data_row:
        data_shape = pickle.loads(str(data_row[0]))
        data_dtype = pickle.loads(str(data_row[1]))
        return data_shape, data_dtype
    return None


class DbDatastore(object):

    def __init__(self, data_reference_handler, db_filename, create=True, storage_data=None):
        self.conn = sqlite3.connect(db_filename, check_same_thread=False)
        self.data_reference_handler = data_reference_handler  # may be None for testing only
        # item map is used during item construction.
        self.__item_map = {}
        self.disconnected = False
        # turn off full synchronization. see http://www.sqlite.org/faq.html#q19
        # this drastically improves write performance, at the cost of database
        # integrity in the case of OS crash or power loss. this has no effect on
        # application crashes.
        self.conn.execute("PRAGMA synchronous = OFF")
        if storage_data:
            self.from_data(storage_data)
        elif create:
            self.create()

    def close(self):
        self.conn.close()
        self.conn = None

    def __get_initialized(self):
        c = self.conn.cursor()
        c.execute("SELECT uuid FROM nodes WHERE refcount=0")
        return c.fetchone() is not None
    initialized = property(__get_initialized)

    def set_disconnected(self, disconnected):
        self.disconnected = disconnected

    def execute(self, c, stmt, args=None, log=False):
        if args:
            c.execute(stmt, args)
            if log:
                logging.debug("%s [%s]", stmt, args)
        else:
            c.execute(stmt)
            if log:
                logging.debug("%s", stmt)

    # used for testing
    def to_data(self):
        # save out to string
        string_file = StringIO.StringIO()
        for line in self.conn.iterdump():
            string_file.write('%s\n' % line)
        string_file.seek(0)
        db_data = string_file.read()
        data_data = self.data_reference_handler.to_data() if hasattr(self.data_reference_handler, "to_data") else None
        return { "db": db_data, "data": data_data }

    # used for testing
    def from_data(self, d):
        self.conn.cursor().executescript(d["db"])
        self.conn.commit()
        self.conn.row_factory = sqlite3.Row
        if hasattr(self.data_reference_handler, "from_data"):
            self.data_reference_handler.from_data(d["data"])

    def print_counts(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM nodes")
        logging.debug("nodes: %s", c.fetchone()[0])
        c.execute("SELECT COUNT(*) FROM properties")
        logging.debug("properties: %s", c.fetchone()[0])
        c.execute("SELECT COUNT(*) FROM data_references")
        logging.debug("data_references: %s", c.fetchone()[0])
        c.execute("SELECT COUNT(*) FROM relationships")
        logging.debug("relationships: %s", c.fetchone()[0])
        c.execute("SELECT COUNT(*) FROM items")
        logging.debug("items: %s", c.fetchone()[0])

    def set_root(self, root):
        self.__make_node(root.uuid, root.storage_type)

    def create(self):
        if not self.disconnected:
            c = self.conn.cursor()
            self.execute(c, "CREATE TABLE IF NOT EXISTS nodes(uuid STRING, type STRING, refcount INTEGER, PRIMARY KEY(uuid))")
            self.execute(c, "CREATE TABLE IF NOT EXISTS properties(uuid STRING, key STRING, value BLOB, PRIMARY KEY(uuid, key))")
            self.execute(c, "CREATE TABLE IF NOT EXISTS data_references(uuid STRING, key STRING, shape BLOB, dtype BLOB, reference_type STRING, reference STRING, PRIMARY KEY(uuid, key))")
            self.execute(c, "CREATE TABLE IF NOT EXISTS relationships(parent_uuid STRING, key STRING, item_index INTEGER, item_uuid STRING, PRIMARY KEY(parent_uuid, key, item_index))")
            self.execute(c, "CREATE TABLE IF NOT EXISTS items(parent_uuid STRING, key STRING, item_uuid STRING, PRIMARY KEY(parent_uuid, key))")
            self.execute(c, "CREATE TABLE IF NOT EXISTS version(version INTEGER, PRIMARY KEY(version))")
            self.execute(c, "INSERT OR REPLACE INTO version (version) VALUES (?)", (10, ))
            self.conn.commit()

    # keep. used for testing
    def find_node_or_none(self, item):
        return self.find_node(item)

    # keep. used for testing
    def find_node(self, item):
        c = self.conn.cursor()
        self.execute(c, "SELECT * FROM nodes WHERE uuid = ?", (str(item.uuid), ))
        node = c.fetchone()
        return node

    def __make_node(self, uuid, type):
        c = self.conn.cursor()
        self.execute(c, "SELECT * FROM nodes WHERE uuid = ?", (str(uuid), ))
        node = c.fetchone()
        if node:
            return node
        else:
            self.execute(c, "INSERT INTO nodes (uuid, type, refcount) VALUES (?, ?, 0)", (str(uuid), str(type) ))
            self.execute(c, "SELECT * FROM nodes WHERE uuid = ?", (str(uuid), ))
            node = c.fetchone()
            return node

    def __add_node_ref(self, uuid_):
        c = self.conn.cursor()
        self.execute(c, "UPDATE nodes SET refcount=refcount+1 WHERE uuid = ?", (str(uuid_), ))

    def __remove_node_ref(self, uuid_, ignore_refcount=False):
        c = self.conn.cursor()
        if not ignore_refcount:
            self.execute(c, "UPDATE nodes SET refcount=refcount-1 WHERE uuid = ?", (str(uuid_), ))
            self.execute(c, "SELECT refcount FROM nodes WHERE uuid = ?", (str(uuid_), ))
            refcount = c.fetchone()[0]
        if ignore_refcount or refcount == 0:
            # remove properties
            self.execute(c, "DELETE FROM properties WHERE uuid = ?", (str(uuid_), ))
            # remove data
            self.__erase_data_reference(c, uuid_)
            # remove single items
            self.execute(c, "SELECT item_uuid FROM items WHERE parent_uuid=?", (str(uuid_), ))
            for row in c.fetchall():
                item_uuid = row[0]
                self.__remove_node_ref(uuid.UUID(item_uuid))
            self.execute(c, "DELETE FROM items WHERE parent_uuid=?", (str(uuid_), ))
            # remove relationships.
            self.execute(c, "SELECT item_uuid FROM relationships WHERE parent_uuid = ?", (str(uuid_), ))
            for row in c.fetchall():
                item_uuid = row[0]
                self.__remove_node_ref(uuid.UUID(item_uuid))
            self.execute(c, "DELETE FROM relationships WHERE parent_uuid = ?", (str(uuid_), ))
            if not ignore_refcount:
                self.execute(c, "DELETE FROM nodes WHERE uuid = ?", (str(uuid_), ))

    # remove this item from all other tables. used for db migration and cleanup after corruption.
    def destroy_node_ref(self, uuid_):
        c = self.conn.cursor()
        self.execute(c, "DELETE FROM data_references WHERE uuid=?", (str(uuid_), ))
        self.execute(c, "DELETE FROM items WHERE item_uuid=?", (str(uuid_), ))
        self.execute(c, "SELECT item_uuid FROM items WHERE parent_uuid=?", (str(uuid_), ))
        for row in c.fetchall():
            item_uuid = row[0]
            self.destroy_node_ref(uuid.UUID(item_uuid))
        self.execute(c, "DELETE FROM items WHERE parent_uuid=?", (str(uuid_), ))
        self.execute(c, "DELETE FROM nodes WHERE uuid=?", (str(uuid_), ))
        self.execute(c, "DELETE FROM properties WHERE uuid=?", (str(uuid_), ))
        self.execute(c, "DELETE FROM relationships WHERE item_uuid=?", (str(uuid_), ))
        self.execute(c, "SELECT item_uuid FROM relationships WHERE parent_uuid=?", (str(uuid_), ))
        for row in c.fetchall():
            item_uuid = row[0]
            self.destroy_node_ref(uuid.UUID(item_uuid))
        self.execute(c, "DELETE FROM relationships WHERE parent_uuid=?", (str(uuid_), ))

    def erase_object(self, object):
        self.__remove_node_ref(object.uuid, True)

    def __erase_data_reference(self, c, uuid_):
        self.execute(c, "SELECT reference_type, reference FROM data_references WHERE uuid=?", (str(uuid_), ))
        for row in c.fetchall():
            self.data_reference_handler.remove_data_reference(row[0], row[1])
        self.execute(c, "DELETE FROM data_references WHERE uuid = ?", (str(uuid_), ))

    def set_type(self, item, type):
        if not self.disconnected:
            c = self.conn.cursor()
            self.execute(c, "UPDATE nodes SET type=? WHERE uuid = ?", (type, str(item.uuid), ))
            self.conn.commit()

    def set_item(self, parent, key, item):
        if not self.disconnected:
            c = self.conn.cursor()
            if not self.find_node_or_none(item):
                node = self.__make_node(item.uuid, item.storage_type)
                item.write()
            self.execute(c, "INSERT OR REPLACE INTO items (parent_uuid, key, item_uuid) VALUES (?, ?, ?)", (str(parent.uuid), key, str(item.uuid), ))
            self.__add_node_ref(item.uuid)
            self.conn.commit()

    def clear_item(self, parent, key):
        if not self.disconnected:
            c = self.conn.cursor()
            self.execute(c, "SELECT item_uuid FROM items WHERE parent_uuid=? AND key=?", (str(parent.uuid), key, ))
            item_uuid = uuid.UUID(c.fetchone()[0])
            self.execute(c, "DELETE FROM items WHERE parent_uuid=? AND key=?", (str(parent.uuid), key, ))
            self.__remove_node_ref(item_uuid)
            self.conn.commit()

    # don't let incorrect item_indexes go into database
    def __item_index_integrity_check(self, c, parent_uuid):
        self.execute(c, "SELECT COUNT(*), MAX(item_index), MIN(item_index) FROM relationships WHERE parent_uuid=? and key='data_items'", (str(parent_uuid), ))
        results = c.fetchone()
        if results[0] > 0:
            assert results[2] == 0
            assert results[1] == results[0] - 1

    def insert_item(self, parent, key, item, before):
        if not self.disconnected:
            c = self.conn.cursor()
            if not self.find_node_or_none(item):
                node = self.__make_node(item.uuid, item.storage_type)
                item.write()
            # 1 2 3 ^ 4 5 6 => 1 2 3 -5 -6 -7 => 1 2 3 5 6 7 => 1 2 3 4 5 6 7
            self.execute(c, "UPDATE relationships SET item_index = -(item_index + 1) WHERE parent_uuid=? AND key=? AND item_index >= ?", (str(parent.uuid), key, before, ))
            self.execute(c, "UPDATE relationships SET item_index = -item_index WHERE parent_uuid=? AND key=? AND item_index < -?", (str(parent.uuid), key, before, ))
            self.execute(c, "INSERT INTO relationships (parent_uuid, key, item_index, item_uuid) VALUES (?, ?, ?, ?)", (str(parent.uuid), key, before, str(item.uuid), ))
            self.__add_node_ref(item.uuid)
            self.__item_index_integrity_check(c, parent.uuid)
            self.conn.commit()

    def remove_item(self, parent, key, index):
        if not self.disconnected:
            c = self.conn.cursor()
            self.execute(c, "SELECT item_uuid FROM relationships WHERE parent_uuid=? AND key=? AND item_index=?", (str(parent.uuid), key, index, ))
            item_uuid = uuid.UUID(c.fetchone()[0])
            self.execute(c, "DELETE FROM relationships WHERE parent_uuid=? AND key=? AND item_index=?", (str(parent.uuid), key, index, ))
            # 1 2 3 (4) 5 6 7 => 1 2 3 -4 -5 -6 => 1 2 3 4 5 6
            self.execute(c, "UPDATE relationships SET item_index = -(item_index - 1) WHERE parent_uuid=? AND key=? AND item_index > ?", (str(parent.uuid), key, index, ))
            self.execute(c, "UPDATE relationships SET item_index = -item_index WHERE parent_uuid=? AND key=? AND item_index <= -?", (str(parent.uuid), key, index, ))
            self.__remove_node_ref(item_uuid)
            self.__item_index_integrity_check(c, parent.uuid)
            self.conn.commit()

    def set_property(self, item, key, value):
        if not self.disconnected:
            c = self.conn.cursor()
            self.execute(c, "INSERT OR REPLACE INTO properties (uuid, key, value) VALUES (?, ?, ?)", (str(item.uuid), key, sqlite3.Binary(pickle.dumps(value, pickle.HIGHEST_PROTOCOL)), ))
            self.conn.commit()

    def write_data_reference(self, data, reference_type, reference, file_datetime):
        self.data_reference_handler.write_data_reference(data, reference_type, reference, file_datetime)

    def write_properties(self, properties, reference_type, reference, file_datetime):
        self.data_reference_handler.write_properties(properties, reference_type, reference, file_datetime)

    # NOTE: parent_nodes are uuid strings for this class

    def find_root_node(self, type):
        c = self.conn.cursor()
        c.execute("SELECT uuid FROM nodes WHERE type=? AND refcount=0", (type, ))
        uuid_ = c.fetchone()[0]
        return uuid_, uuid.UUID(uuid_)

    def find_parent_node(self, item):
        return str(item.uuid)

    def get_version(self):
        c = self.conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS version(version INTEGER, PRIMARY KEY(version))")
        c.execute("SELECT COUNT(*) FROM version")
        has_version = c.fetchone()[0] == 1
        if has_version:
            c.execute("SELECT version FROM version")
            return c.fetchone()[0]
        return 0  # no version, it's zero

    def build_item(self, uuid_, node):
        uuid_str = str(uuid_)
        item = None
        if uuid_str not in self.__item_map:
            from nion.swift.model import DataGroup
            from nion.swift.model import DataItem
            build_map = {
                "data-group": DataGroup.DataGroup,
                "data-item": DataItem.DataItem,
            }
            c = self.conn.cursor()
            c.execute("SELECT type FROM nodes WHERE uuid=?", (uuid_str, ))
            type = c.fetchone()[0]
            if type in build_map:
                item = build_map[type].build(self, node)
                item._set_uuid(uuid_)
            if item:
                self.__item_map[uuid_str] = item
            else:
                logging.debug("Unable to build %s (%s)", type, uuid_str)
        else:
            item = self.__item_map[uuid_str]
        return item

    def has_data(self, parent_node, key):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM data_references WHERE uuid=? AND key=?", (str(parent_node), key, ))
        return c.fetchone()[0] > 0

    def has_item(self, parent_node, key):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM items WHERE uuid=? AND key=?", (str(parent_node), key, ))
        return c.fetchone()[0] > 0

    def has_relationship(self, parent_node, key):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM relationships WHERE parent_uuid=? AND key=?", (str(parent_node), key, ))
        return c.fetchone()[0] > 0

    def get_item(self, parent_node, key, default_value=None):
        c = self.conn.cursor()
        c.execute("SELECT item_uuid FROM items WHERE parent_uuid=? AND key=?", (str(parent_node), key, ))
        value_row = c.fetchone()
        if value_row is not None:
            uuid_str = value_row[0]
            item = self.build_item(uuid.UUID(uuid_str), uuid_str)
            return item
        return default_value

    def check_integrity(self, raise_exception=False):
        # check for duplicated items in relationships.
        # if the relationship table gets tainted with duplicate items,
        # the best thing to do is just read them in, keeping only the one with
        # the earliest index, and rewrite the indexes. there is no integrity
        # check based on primary keys to do at the db level since the index/item_uuid
        # will always be unique even when the same item_uuid exists in multiple
        # indexes.
        c = self.conn.cursor()
        c.execute("SELECT parent_uuid, key, item_index, item_uuid, COUNT(*) AS c FROM relationships GROUP BY parent_uuid, key, item_uuid HAVING c != 1")
        for row in c.fetchall():
            logging.debug("Duplicate item in relationship from %s to %s (%s[%s]) x%s", row[0], row[3], row[1], row[2], row[4])
            if raise_exception:
                raise ValueError()
        # find dead nodes where the type is NULL and cannot be built. destroy them.
        c.execute("SELECT uuid FROM nodes WHERE type IS Null")
        for row in c.fetchall():
            uuid_str = row[0]
            logging.debug("Removing malformed item %s", uuid_str)
            self.destroy_node_ref(uuid.UUID(uuid_str))
        # find relationships where the relationship indexes are messed up. fix 'em.
        c.execute("SELECT parent_uuid, key, COUNT(*) FROM relationships GROUP BY parent_uuid, key HAVING COUNT(*) != MAX(item_index)+1")
        for row in c.fetchall():
            uuid_str = row[0]
            key = row[1]
            count = row[2]
            logging.debug("Fixing indexes %s %s", uuid_str, key)
            self.execute(c, "SELECT item_index FROM relationships WHERE parent_uuid=? AND key=? ORDER BY item_index", (uuid_str, key))
            index = 0
            for subrow in c.fetchall():
                item_index = subrow[0]
                self.execute(c, "UPDATE relationships SET item_index=? WHERE parent_uuid=? AND key=? AND item_index=?", (index, uuid_str, key, item_index))
                index += 1
        self.conn.commit()

    def get_items(self, parent_node, key, builder=None):
        builder = builder if builder else self.build_item
        c = self.conn.cursor()
        c.execute("SELECT item_uuid FROM relationships WHERE parent_uuid=? AND key=? ORDER BY item_index ASC", (str(parent_node), key, ))
        items = []
        for row in c.fetchall():
            uuid_str = row[0]
            item = builder(uuid.UUID(uuid_str), uuid_str)
            if item:
                # this should be fixed in the integrity check, but until that
                # is fully implemented, just skip it.
                if item not in items:
                    items.append(item)
        #items = [self.build_item(row[0]) for row in c.fetchall()]
        return items

    def get_property(self, parent_node, key, default_value=None):
        c = self.conn.cursor()
        c.execute("SELECT value FROM properties WHERE uuid=? AND key=?", (parent_node, key, ))
        value_row = c.fetchone()
        if value_row is not None:
            return pickle.loads(str(value_row[0]))
        else:
            return default_value

    def get_data_reference(self, parent_node, key):
        c = self.conn.cursor()
        return db_get_data_reference(c, parent_node, key)

    def load_data_reference(self, key, reference_type, reference):
        return self.data_reference_handler.load_data_reference(reference_type, reference)

    def get_data_shape_and_dtype(self, parent_node, key):
        c = self.conn.cursor()
        return db_get_data_shape_and_dtype(c, parent_node, key)

    # alternate, direct interface to db

    def find_data_item_tuples(self):
        tuples = self.data_reference_handler.find_data_item_tuples()
        if tuples is None:
            tuples = list()
            c = self.conn.cursor()
            c.execute("SELECT uuid FROM nodes WHERE type='data-item'")
            for row in c.fetchall():
                item_uuid = uuid.UUID(row[0])
                properties = self.get_root_property(item_uuid, "properties")
                reference_type, reference = self.get_root_data_reference(item_uuid, "master_data")
                tuples.append((item_uuid, properties, reference_type, reference))
                #logging.debug("TWO %s", (item_uuid, properties, reference_type, reference))
        return tuples

    def find_root_item_uuids(self, key):
        c = self.conn.cursor()
        c.execute("SELECT uuid FROM nodes WHERE type=?", (key, ))
        item_uuids = []
        for row in c.fetchall():
            item_uuids.append(uuid.UUID(row[0]))
        return item_uuids

    def add_root_item_uuid(self, key, item_uuid):
        if not self.disconnected:
            c = self.conn.cursor()
            node = self.__make_node(item_uuid, key)
            self.__add_node_ref(item_uuid)
            self.conn.commit()

    def remove_root_item_uuid(self, key, item_uuid, reference_type, reference):
        if not self.disconnected:
            c = self.conn.cursor()
            # remove properties, node, data_reference
            self.execute(c, "DELETE FROM properties WHERE uuid = ?", (str(item_uuid), ))
            self.execute(c, "DELETE FROM nodes WHERE uuid = ?", (str(item_uuid), ))
            self.execute(c, "DELETE FROM data_references WHERE uuid = ?", (str(item_uuid), ))
            self.conn.commit()
            # and the file
            self.data_reference_handler.remove_data_reference(reference_type, reference)

    def get_root_property(self, item_uuid, key):
        c = self.conn.cursor()
        c.execute("SELECT value FROM properties WHERE uuid=? AND key=?", (str(item_uuid), key, ))
        value_row = c.fetchone()
        if value_row is not None:
            return pickle.loads(str(value_row[0]))
        return None

    def set_root_properties(self, item_uuid, properties, reference, file_datetime):
        if not self.disconnected:
            c = self.conn.cursor()
            self.execute(c, "INSERT OR REPLACE INTO properties (uuid, key, value) VALUES (?, ?, ?)", (str(item_uuid), "properties", sqlite3.Binary(pickle.dumps(properties, pickle.HIGHEST_PROTOCOL)), ))
            self.conn.commit()
        # write to the file too
        self.write_properties(properties, "relative_file", reference, file_datetime)

    def set_root_data(self, item_uuid, data, data_shape, data_dtype, reference, file_datetime):
        if not self.disconnected:
            c = self.conn.cursor()
            db_write_data_reference(c, item_uuid, "master_data", data_shape, data_dtype, "relative_file", reference)
            self.conn.commit()
        if data is not None:
            self.write_data_reference(data, "relative_file", reference, file_datetime)

    def get_root_data_reference(self, item_uuid, key):
        c = self.conn.cursor()
        return db_get_data_reference(c, item_uuid, key)


class DbDatastoreProxy(object):

    def __init__(self, data_reference_handler, db_filename, create=True, storage_data=None):
        self.__datastore = None
        self.__queue = Queue.Queue()
        self.__started_event = threading.Event()
        self.__thread = threading.Thread(target=self.__run, args=[data_reference_handler, db_filename, create, storage_data])
        self.__thread.daemon = True
        self._throttling = 0  # for testing to slow down db operations, in seconds
        self.__thread.start()
        self.__started_event.wait()

    def close(self):
        self.__queue.put((None, None, None))
        self.__queue.join()

    def __get_initialized(self):
        return self.__datastore.initialized
    initialized = property(__get_initialized)

    def __get_conn(self):
        return self.__datastore.conn
    conn = property(__get_conn)

    def __get_disconnected(self):
        return self.__datastore.disconnected
    def __set_disconnected(self, disconnected):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_disconnected, self.__datastore, disconnected), event, "set_disconnected"))
        event.wait()
    disconnected = property(__get_disconnected, __set_disconnected)

    def __run(self, data_reference_handler, db_filename, create, storage_data):
        self.__datastore = DbDatastore(data_reference_handler, db_filename, create=create, storage_data=storage_data)
        self.__started_event.set()
        while True:
            action = self.__queue.get()
            item = action[0]
            event = action[1]
            action_name = action[2]
            if item:
                try:
                    #logging.debug("EXECUTE %s", action_name)
                    item()
                    time.sleep(self._throttling)
                except Exception as e:
                    import traceback
                    logging.debug("DB Error: %s", e)
                    traceback.print_exc()
                    traceback.print_stack()
                finally:
                    #logging.debug("FINISH")
                    event.set()
            self.__queue.task_done()
            if not item:
                break
        self.__datastore.close()
        self.__datastore = None

    def to_data(self):
        self.__queue.join()
        return self.__datastore.to_data()

    def create(self):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.create, self.__datastore), event, "create"))
        #event.wait()

    def set_root(self, root):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_root, self.__datastore, root), event, "set_root"))
        #event.wait()

    def erase_object(self, object):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.erase_object, self.__datastore, object), event, "erase_object"))
        #event.wait()

    def set_type(self, item, type):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_type, self.__datastore, item, type), event, "set_type"))
        #event.wait()

    def set_item(self, parent, key, item):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_item, self.__datastore, parent, key, item), event, "set_item"))
        #event.wait()

    def clear_item(self, parent, key):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.clear_item, self.__datastore, parent, key), event, "clear_item"))
        #event.wait()

    def insert_item(self, parent, key, item, before):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.insert_item, self.__datastore, parent, key, item, before), event, "insert_item"))
        #event.wait()

    def remove_item(self, parent, key, index):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.remove_item, self.__datastore, parent, key, index), event, "remove_item"))
        #event.wait()

    def set_property(self, item, key, value):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_property, self.__datastore, item, key, value), event, "set_property"))
        #event.wait()

    def write_data_reference(self, data, reference_type, reference, file_datetime):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.write_data_reference, self.__datastore, data, reference_type, reference, file_datetime), event, "write_data_reference"))
        #event.wait()

    def write_properties(self, properties, reference_type, reference, file_datetime):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.write_properties, self.__datastore, properties, reference_type, reference, file_datetime), event, "write_properties"))
        #event.wait()

    def destroy_node_ref(self, uuid_):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.destroy_node_ref, self.__datastore, uuid_), event, "destroy_node_ref"))
        #event.wait()

    # these methods read data. they must wait for the queue to finish.

    def find_root_node(self, type):
        self.__queue.join()
        return self.__datastore.find_root_node(type)

    def find_parent_node(self, item):
        self.__queue.join()
        return self.__datastore.find_parent_node(item)

    def get_version(self):
        self.__queue.join()
        return self.__datastore.get_version()

    def has_data(self, parent_node, key):
        self.__queue.join()
        return self.__datastore.has_data(parent_node, key)

    def has_item(self, parent_node, key):
        self.__queue.join()
        return self.__datastore.has_item(parent_node, key)

    def has_relationship(self, parent_node, key):
        self.__queue.join()
        return self.__datastore.has_relationship(parent_node, key)

    def get_item(self, parent_node, key, default_value=None):
        self.__queue.join()
        return self.__datastore.get_item(parent_node, key, default_value)

    def check_integrity(self):
        self.__queue.join()
        self.__datastore.check_integrity()

    def get_items(self, parent_node, key):
        self.__queue.join()
        return self.__datastore.get_items(parent_node, key)

    def get_property(self, parent_node, key, default_value=None):
        self.__queue.join()
        return self.__datastore.get_property(parent_node, key, default_value)

    def get_data_reference(self, parent_node, key):
        self.__queue.join()
        return self.__datastore.get_data_reference(parent_node, key)

    def load_data_reference(self, key, reference_type, reference):
        self.__queue.join()
        return self.__datastore.load_data_reference(key, reference_type, reference)

    def get_data_shape_and_dtype(self, parent_node, key):
        self.__queue.join()
        return self.__datastore.get_data_shape_and_dtype(parent_node, key)

    # alternate, direct interface to db

    def find_data_item_tuples(self):
        self.__queue.join()
        return self.__datastore.find_data_item_tuples()

    def add_root_item_uuid(self, key, item_uuid):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.add_root_item_uuid, self.__datastore, key, item_uuid), event, "add_root_item_uuid"))
        #event.wait()

    def remove_root_item_uuid(self, key, item_uuid, reference_type, reference):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.remove_root_item_uuid, self.__datastore, key, item_uuid, reference_type, reference), event, "remove_root_item_uuid"))
        #event.wait()

    def get_root_property(self, item_uuid, key):
        self.__queue.join()
        return self.__datastore.get_root_property(item_uuid, key)

    def set_root_properties(self, item_uuid, properties, reference, file_datetime):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_root_properties, self.__datastore, item_uuid, properties, reference, file_datetime), event, "set_root_properties"))
        #event.wait()

    def set_root_data(self, item_uuid, data, data_shape, data_dtype, reference, file_datetime):
        event = threading.Event()
        self.__queue.put((functools.partial(DbDatastore.set_root_data, self.__datastore, item_uuid, data, data_shape, data_dtype, reference, file_datetime), event, "set_root_data"))
        #event.wait()

    def get_root_data_reference(self, item_uuid, key):
        self.__queue.join()
        return self.__datastore.get_root_data_reference(item_uuid, key)


class DictStorageCache(object):
    def __init__(self):
        self.__cache = dict()
        self.__cache_dirty = dict()

    def set_cached_value(self, object, key, value, dirty=False):
        cache = self.__cache.setdefault(object.uuid, dict())
        cache_dirty = self.__cache_dirty.setdefault(object.uuid, dict())
        cache[key] = value
        cache_dirty[key] = False

    def get_cached_value(self, object, key, default_value=None):
        cache = self.__cache.setdefault(object.uuid, dict())
        return cache.get(key, default_value)

    def remove_cached_value(self, object, key):
        cache = self.__cache.setdefault(object.uuid, dict())
        cache_dirty = self.__cache_dirty.setdefault(object.uuid, dict())
        if key in cache:
            del cache[key]
        if key in cache_dirty:
            del cache_dirty[key]

    def is_cached_value_dirty(self, object, key):
        cache_dirty = self.__cache_dirty.setdefault(object.uuid, dict())
        return cache_dirty[key] if key in cache_dirty else True

    def set_cached_value_dirty(self, object, key, dirty=True):
        cache_dirty = self.__cache_dirty.setdefault(object.uuid, dict())
        cache_dirty[key] = dirty


class DbStorageCache(object):
    def __init__(self, cache_filename):
        self.__queue = Queue.Queue()
        self.__queue_lock = threading.RLock()
        self.__started_event = threading.Event()
        self.__thread = threading.Thread(target=self.__run, args=[cache_filename])
        self.__thread.daemon = True
        self.__thread.start()
        self.__started_event.wait()

    def close(self):
        with self.__queue_lock:
            assert self.__queue is not None
            self.__queue.put((None, None, None, None))
            self.__queue.join()
            self.__queue = None

    def __run(self, cache_filename):
        self.conn = sqlite3.connect(cache_filename)
        self.conn.execute("PRAGMA synchronous = OFF")
        self.create()
        self.__started_event.set()
        while True:
            action = self.__queue.get()
            item, result, event, action_name = action
            #logging.debug("item %s  result %s  event %s  action %s", item, result, event, action_name)
            if item:
                try:
                    #logging.debug("EXECUTE %s", action_name)
                    start = time.time()
                    if result is not None:
                        result.append(item())
                    else:
                        item()
                    elapsed = time.time() - start
                    #logging.debug("ELAPSED %s", elapsed)
                except Exception as e:
                    import traceback
                    logging.debug("DB Error: %s", e)
                    traceback.print_exc()
                    traceback.print_stack()
                finally:
                    #logging.debug("FINISH")
                    if event:
                        event.set()
            self.__queue.task_done()
            if not item:
                break
        self.conn.close()
        self.conn = None

    def create(self):
        with self.conn:
            self.execute("CREATE TABLE IF NOT EXISTS cache(uuid STRING, key STRING, value BLOB, dirty INTEGER, PRIMARY KEY(uuid, key))")

    def execute(self, stmt, args=None, log=False):
        if args:
            self.last_result = self.conn.execute(stmt, args)
            if log:
                logging.debug("%s [%s]", stmt, args)
        else:
            self.conn.execute(stmt)
            if log:
                logging.debug("%s", stmt)

    def __set_cached_value(self, object, key, value, dirty=False):
        with self.conn:
            self.execute("INSERT OR REPLACE INTO cache (uuid, key, value, dirty) VALUES (?, ?, ?, ?)", (str(object.uuid), key, sqlite3.Binary(pickle.dumps(value, pickle.HIGHEST_PROTOCOL)), 1 if dirty else 0))

    def __get_cached_value(self, object, key, default_value=None):
        self.execute("SELECT value FROM cache WHERE uuid=? AND key=?", (str(object.uuid), key))
        value_row = self.last_result.fetchone()
        if value_row is not None:
            result = pickle.loads(str(value_row[0]))
            return result
        else:
            return default_value

    def __remove_cached_value(self, object, key):
        with self.conn:
            self.execute("DELETE FROM cache WHERE uuid=? AND key=?", (str(object.uuid), key))

    def __is_cached_value_dirty(self, object, key):
        self.execute("SELECT dirty FROM cache WHERE uuid=? AND key=?", (str(object.uuid), key))
        value_row = self.last_result.fetchone()
        if value_row is not None:
            return value_row[0] != 0
        else:
            return True

    def __set_cached_value_dirty(self, object, key, dirty=True):
        with self.conn:
            self.execute("UPDATE cache SET dirty=? WHERE uuid=? AND key=?", (1 if dirty else 0, str(object.uuid), key))

    def set_cached_value(self, object, key, value, dirty=False):
        event = threading.Event()
        with self.__queue_lock:
            queue = self.__queue
        if queue:
            queue.put((functools.partial(self.__set_cached_value, object, key, value, dirty), None, event, "set_cached_value"))
        #event.wait()

    def get_cached_value(self, object, key, default_value=None):
        event = threading.Event()
        result = list()
        with self.__queue_lock:
            queue = self.__queue
        if queue:
            queue.put((functools.partial(self.__get_cached_value, object, key, default_value), result, event, "get_cached_value"))
            event.wait()
        return result[0] if len(result) > 0 else None

    def remove_cached_value(self, object, key):
        event = threading.Event()
        with self.__queue_lock:
            queue = self.__queue
        if queue:
            queue.put((functools.partial(self.__remove_cached_value, object, key), None, event, "remove_cached_value"))
        #event.wait()

    def is_cached_value_dirty(self, object, key):
        event = threading.Event()
        result = list()
        with self.__queue_lock:
            queue = self.__queue
        if queue:
            queue.put((functools.partial(self.__is_cached_value_dirty, object, key), result, event, "is_cached_value_dirty"))
            event.wait()
        return result[0]

    def set_cached_value_dirty(self, object, key, dirty=True):
        event = threading.Event()
        with self.__queue_lock:
            queue = self.__queue
        if queue:
            queue.put((functools.partial(self.__set_cached_value_dirty, object, key, dirty), None, event, "set_cached_value_dirty"))
        #event.wait()
