from __future__ import with_statement
import os, tempfile, sys, shutil, types, time, threading, ConfigParser

from ccmlib.cluster import Cluster
from ccmlib.node import Node

LOG_SAVED_DIR="logs"
LAST_LOG = os.path.join(LOG_SAVED_DIR, "last")

LAST_TEST_DIR='last_test_dir'

DEFAULT_DIR='./'
config = ConfigParser.RawConfigParser()
if len(config.read(os.path.expanduser('~/.cassandra-dtest'))) > 0:
    if config.has_option('main', 'default_dir'):
        DEFAULT_DIR=os.path.expanduser(config.get('main', 'default_dir'))

class Tester(object):
    def __get_cluster(self, name='test'):
        self.test_path = tempfile.mkdtemp(prefix='dtest-')
        try:
            version = os.environ['CASSANDRA_VERSION']
            return Cluster(self.test_path, name, cassandra_version=version)
        except KeyError:
            try:
                cdir = os.environ['CASSANDRA_DIR']
            except KeyError:
                cdir = DEFAULT_DIR
            return Cluster(self.test_path, name, cassandra_dir=cdir)

    def __cleanup_cluster(self):
        self.cluster.remove()
        os.rmdir(self.test_path)
        os.remove(LAST_TEST_DIR)

    def setUp(self):
        # cleaning up if a previous execution didn't trigger tearDown (which
        # can happen if it is interrupted by KeyboardInterrupt)
        # TODO: move that part to a generic fixture
        if os.path.exists(LAST_TEST_DIR):
            with open(LAST_TEST_DIR) as f:
                self.test_path = f.readline().strip('\n')
                name = f.readline()
                try:
                    self.cluster = Cluster.load(self.test_path, name)
                    # Avoid waiting too long for node to be marked down
                    self.__cleanup_cluster()
                except IOError:
                    # after a restart, /tmp will be emptied so we'll get an IOError when loading the old cluster here
                    pass

        self.cluster = self.__get_cluster()
        # the failure detector can be quite slow in such tests with quick start/stop
        self.cluster.set_configuration_options(values={'phi_convict_threshold': 5})
        with open(LAST_TEST_DIR, 'w') as f:
            f.write(self.test_path + '\n')
            f.write(self.cluster.name)
        self.connections = []
        self.runners = []

    def tearDown(self):
        for con in self.connections:
            con.close()

        for runner in self.runners:
            try:
                runner.stop()
            except:
                pass

        failed = sys.exc_info() != (None, None, None)
        try:
            for node in self.cluster.nodelist():
                errors = [ msg for msg, i in node.grep_log("ERROR")]
                if len(errors) is not 0:
                    failed = True
                    raise AssertionError('Unexpected error in %s node log: %s' % (node.name, errors))
        finally:
            try:
                if failed:
                    # means the test failed. Save the logs for inspection.
                    if not os.path.exists(LOG_SAVED_DIR):
                        os.mkdir(LOG_SAVED_DIR)
                    logs = [ (node.name, node.logfilename()) for node in self.cluster.nodes.values() ]
                    if len(logs) is not 0:
                        basedir = str(int(time.time() * 1000))
                        dir = os.path.join(LOG_SAVED_DIR, basedir)
                        os.mkdir(dir)
                        for name, log in logs:
                            shutil.copyfile(log, os.path.join(dir, name + ".log"))
                        if os.path.exists(LAST_LOG):
                            os.unlink(LAST_LOG)
                        os.symlink(basedir, LAST_LOG)
            except Exception as e:
                    print "Error saving log:", str(e)
            finally:
                self.__cleanup_cluster()

    def cql_connection(self, node, keyspace=None):
        import cql
        host, port = node.network_interfaces['thrift']
        con = cql.connect(host, port, keyspace)
        self.connections.append(con)
        return con

    def create_ks(self, cursor, name, rf):
        query = 'CREATE KEYSPACE %s WITH strategy_class=%s AND %s'
        if isinstance(rf, types.IntType):
            # we assume simpleStrategy
            cursor.execute(query % (name, 'SimpleStrategy', 'strategy_options:replication_factor=%d' % rf))
        else:
            assert len(rf) != 0, "At least one datacenter/rf pair is needed"
            # we assume networkTopolyStrategy
            options = (' AND ').join([ 'strategy_options:%s=%d' % (d, r) for d, r in rf.iteritems() ])
            cursor.execute(query % (name, 'NetworkTopologyStrategy', options))
        cursor.execute('USE %s' % name)

    # We default to UTF8Type because it's simpler to use in tests
    def create_cf(self, cursor, name, key_type="varchar", comparator="UTF8Type", validation="UTF8Type", read_repair=None, compression=None, gc_grace=None, columns=None):
        additional_columns = ""
        if columns is not None:
            for k, v in columns.items():
                additional_columns = "%s, %s %s" % (additional_columns, k, v)
        query = 'CREATE COLUMNFAMILY %s (key %s PRIMARY KEY%s) WITH comparator=%s AND default_validation=%s' % (name, key_type, additional_columns, comparator, validation)
        if read_repair is not None:
            query = '%s AND read_repair_chance=%f' % (query, read_repair)
        if compression is not None:
            query = '%s AND compression_options={sstable_compression:%sCompressor}' % (query, compression)
        if gc_grace is not None:
            query = '%s AND gc_grace_seconds=%d' % (query, gc_grace)
        cursor.execute(query)

    def go(self, func):
        runner = Runner(func)
        self.runners.append(runner)
        runner.start()
        return runner

class Runner(threading.Thread):
    def __init__(self, func):
        threading.Thread.__init__(self)
        self.__func = func
        self.__error = None
        self.__stopped = False
        self.daemon = True

    def run(self):
        i = 0
        while True:
            if self.__stopped:
                return
            try:
                self.__func(i)
            except Exception as e:
                self.__error = e
                return
            i = i + 1

    def stop(self):
        self.__stopped = True
        self.join()
        if self.__error is not None:
            raise self.__error

    def check(self):
        if self.__error is not None:
            raise self.__error
