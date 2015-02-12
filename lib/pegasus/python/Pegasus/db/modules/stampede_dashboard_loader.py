"""
Load input into the Stampede DB schema via a SQLAlchemy interface.  This
is an nl_load module which MUST be invoked with the command-line pair
connString='SQLAlchemy connection string'.  Example:

nl_parse bp pegasus.db | nl_load stampede_loader connString='sqlite:///pegasusTest.db'

The connection string must be of a format that is accepted as the first arg
of the SQLAlchemy create_engine() function.  The database indicated by the
conection string will be create and populated with tables and indexes
if it does not exist.  If it does exist, it will merely be connected to
and the SQLAlchemy object mappings will be initialized.

This module does not produce any output other than loading the BP data into
the Stampede DB.

See http://www.sqlalchemy.org/ for details on SQLAlchemy
"""
__rcsid__ = "$Id$"
__author__ = "Monte Goode"
__author__ = "Karan Vahi"

from Pegasus.db.schema.schema_check import ErrorStrings, SchemaCheck, SchemaVersionError
from Pegasus.db.schema.stampede_dashboard_schema import *
from Pegasus.db.modules import Analyzer as BaseAnalyzer
from Pegasus.db.modules import SQLAlchemyInit
from Pegasus.netlogger import util
import time

class Analyzer(BaseAnalyzer, SQLAlchemyInit):
    """Load into the Stampede Dashboard SQL schema through SQLAlchemy.

    Parameters:
      - connString {string,None*}: SQLAlchemy connection string.
        The general form of this is
          'dialect+driver://username:password@host:port/database'.
        See the SQLAlchemy docs for details.
        For sqlite, use 'sqlite:///foo.db' for a relative path and
        'sqlite:////path/to/foo.db' (four slashes) for an absolute one.
        When using MySQL, the general form will work, but the library
        expects the database to exist (ie: will not issue CREATE DB)
        but will populate an empty DB with tables/indexes/etc.
    """
    def __init__(self, connString=None, perf='no', batch='no', **kw):
        """Init object

        @type   connString: string
        @param  connString: SQLAlchemy connection string - REQUIRED
        """
        BaseAnalyzer.__init__(self, **kw)

        if connString is None:
            raise ValueError("connString is required")
        try:
            SQLAlchemyInit.__init__(self, connString, initializeToDashboardDB)
        except exc.OperationalError, e:
            self.log.exception(e)
            self.log.error('Error initializing dashboard loader')
            raise RuntimeError

        # "Case" dict to map events to handler methods
        self.eventMap = {
            'dashboard.wf.plan' : self.workflow,
#            'dashboard.wf.map.task_job' : self.task_map,
            'dashboard.xwf.start' : self.workflowstate,
            'dashboard.xwf.end' : self.workflowstate,
        }

        # Dicts for caching FK lookups
        self.wf_id_cache = {}
        self.root_wf_id_cache = {}

        # undocumented performance option
        self._perf = util.as_bool(perf)
        if self._perf:
            self._insert_time, self._insert_num = 0, 0
            self._start_time = time.time()

        # flags and state for batching
        self._batch = util.as_bool(batch)
        self._flush_every = 1
        self._flush_count = 0
        self._last_flush = time.time()

        # caches for batched events
        self._batch_cache = {
            'batch_events' : [],
            'update_events' : [],
            'host_map_events' : []
        }

    def process(self, linedata):
        """
        @type   linedata: dict
        @param  linedata: One line of BP data dict-ified.

        Get the BP dict from the controlling process and dispatch
        to the appropriate method per-event.
        """
        self.log.debug('process: %s', linedata)

        if not self._batch:
            self.check_connection()

        try:
            if self._perf:
                t = time.time()
                self.eventMap[linedata['event']](linedata)
                self._insert_time += (time.time() - t)
                self._insert_num += 1
            else:
                self.eventMap[linedata['event']](linedata)
        except KeyError:
            self.log.error('no handler for event type "%s" defined', linedata['event'])
        except exc.IntegrityError, e:
            # This is raised when an attempted insert violates the
            # schema (unique indexes, etc).
            self.log.error('Insert failed for event "%s" : %s', linedata['event'], e)
            self.session.rollback()
        except exc.OperationalError, e:
            self.log.error('Connection seemingly lost - attempting to refresh')
            self.session.rollback()
            self.check_connection()
            self.process(linedata)

        self.check_flush()

    def linedataToObject(self, linedata, o):
        """
        @type   linedata: dict
        @param  linedata: One line of BP data dict-ified.
        @type   o: instance of mapper class from stampede_dashboard_schema module.
        @param  o: Passed in by the appropriate event handler method.

        Takes the dict of BP linedata, assigns contents to the class o
        as attributes, and does any global type massaging like
        transforming dict strings to numeric types.
        """
        for k,v in linedata.items():
            if k == 'level':
                continue

            # undot
            attr = k.replace('.', '_')

            attr_remap = {
                # workflow
                'xwf_id': 'wf_uuid',
            }

            # remap attr names
            if attr_remap.has_key(attr):
                attr = attr_remap[attr]

            # sanitize argv input
            if attr == 'argv':
                if v != None:
                    v = v.replace("\\", "\\\\" )
                    v = v.replace("'", "\\'")

            try:
                setattr(o, attr, v)
            except:
                self.log.error('unable to process attribute %s with values: %s', k, v)

        # global type re-assignments
        if hasattr(o, 'ts'):
            # make all timestamp values floats
            o.ts = float(o.ts)
        if hasattr(o, 'restart_count') and o.restart_count != None:
            o.restart_count = int(o.restart_count)
        return o

    #############################################
    # Methods to handle batching/flushing
    #############################################

    def reset_flush_state(self):
        """
        Reset the internal flust state if batching.
        """
        if self._batch:
            self.log.debug('Resetting flush state')
            self._flush_count = 0
            self._last_flush = time.time()

    def check_flush(self):
        """
        Check to see if the batch needs to be flushed based on
        either the number of queued inserts or based on time
        since last flush.
        """
        if not self._batch:
            return

        if self._flush_count >= self._flush_every:
            self.hard_flush()
            self.log.debug('Flush: flush count')
            return
        else:
            self._flush_count += 1

        if (time.time() - self._last_flush) > 30:
            self.hard_flush()
            self.log.debug('Flush: time based')

    def check_connection(self, sub=False):
        self.log.debug('Checking connection')
        try:
            self.session.connection().closed
        except exc.OperationalError, e:
            try:
                if not self.session.is_active:
                    self.session.rollback()
                self.log.error('Lost connection - attempting reconnect')
                time.sleep(5)
                self.session.connection().connect()
            except exc.OperationalError, e:
                self.check_connection(sub=True)
            if not sub:
                self.log.warn('Connection re-established')

    def hard_flush(self, batch_flush=True):
        """
        @type   batch_flush: boolean
        @param  batch_flush: Defaults to true.  Is set to false
            when the batch commit hits and integrity error.

        Process queued inserts and flush/commit to the database.
        If the commit fails due to an integrity error, then method
        re-calls itself with setting batch_flush to False which
        causes each insert/object to be committed individually
        so all the "good" inserts can succeed.  This will increase
        the processing time of the batch with the bad data in it.
        """
        if not self._batch:
            return
        self.log.debug('Hard flush')

        self.check_connection()

        if self._perf:
            s = time.time()

        end_event = []

        for event in self._batch_cache['batch_events']:
            if event.event == 'dashboard.xwf.end':
                end_event.append(event)
            if batch_flush:
                self.session.add(event)
            else:
                self.individual_commit(event)

        for event in self._batch_cache['update_events']:
            if batch_flush:
                self.session.merge(event)
            else:
                self.individual_commit(event, merge=True)

        try:
            self.session.commit()
        except exc.IntegrityError, e:
            self.log.error('Integrity error on batch flush: %s - batch will need to be committed per-event which will take longer', e)
            self.session.rollback()
            self.hard_flush(batch_flush=False)
        except exc.OperationalError, e:
            self.log.error('Connection problem during commit: %s - reattempting batch', e)
            self.session.rollback()
            self.hard_flush()

        for host in self._batch_cache['host_map_events']:
            self.map_host_to_job_instance(host)

        for ee in end_event:
            self.flushCaches(ee)
        end_event = []

        # Clear all data structures here.
        for k in self._batch_cache.keys():
            self._batch_cache[k] = []

        self.session.commit()
        self.reset_flush_state()

        if self._perf:
            self.log.info('Hard flush duration', (time.time() - s))

    def individual_commit(self, event, merge=False):
        """
        @type   merge: boolean
        @param  merge: Set to true if the row should be a merge
                rather than a plain insert.

        This gets called by hard_flush if there is a problem
        with a batch commit to commit each object individually.
        """
        try:
            if merge:
                event.merge_to_db(self.session)
            else:
                event.commit_to_db(self.session)
            self.session.expunge(event)
        except exc.IntegrityError, e:
            self.log.error('Insert failed for event %s : %s', event, e)
            self.session.rollback()


    #############################################
    # Methods to handle the various insert events
    #############################################
    def workflow(self, linedata):
        """
        @type   linedata: dict
        @param  linedata: One line of BP data dict-ified.

        Handles a workflow insert event.
        """
        wf = self.linedataToObject(linedata, DashboardWorkflow())
        self.log.debug('workflow: %s', wf)

        wf.timestamp = wf.ts

        is_root = True

        #for time being we don't track these. Karan
#        if wf.root_xwf_id != wf.wf_uuid:
#            is_root = False
#            wf.root_wf_id = self.wf_uuid_to_id(wf.root_xwf_id)
#
#        if wf.parent_wf_id is not None:
#            wf.parent_wf_id = self.wf_uuid_to_id(wf.parent_wf_id)

        # workflow inserts must be explicitly written to db whether
        # batching or not
        wf.commit_to_db(self.session)
        if is_root:
            wf.root_wf_id = self.wf_uuid_to_id(wf.root_xwf_id)
            wf.commit_to_db(self.session)
        if wf.root_wf_id == None:
            self.log.warn('Could not determine root_wf_id for event %s', wf)

    def workflowstate(self, linedata):
        """
        @type   linedata: dict
        @param  linedata: One line of BP data dict-ified.

        Handles a workflowstate insert event.
        """
        wfs = self.linedataToObject(linedata, DashboardWorkflowstate())
        self.log.debug('workflowstate: %s', wfs)

        state = {
            'dashboard.xwf.start': 'WORKFLOW_STARTED',
            'dashboard.xwf.end': 'WORKFLOW_TERMINATED'
        }

        wfs.wf_id = self.wf_uuid_to_id(wfs.wf_uuid)
        wfs.timestamp = wfs.ts
        wfs.state = state[wfs.event]

        if self._batch:
            self._batch_cache['batch_events'].append(wfs)
        else:
            wfs.commit_to_db(self.session)
            if wfs.event == 'dashboard.xwf.end':
                self.flushCaches(wfs)


    ####################################
    # DB helper/lookup/caching functions
    ####################################
    def wf_uuid_to_id(self, wf_uuid):
        """
        @type   wf_uuid: string
        @param  wf_uuid: wf_uuid string from BP logs

        Attempts to retrieve a workflow wf_id PK/FK from cache.  If
        not in cache, retrieve from st_workflow table in DB and cache.
        Cuts down on DB queries during insert processing.
        """
        if not self.wf_id_cache.has_key(wf_uuid):
            query = self.session.query(DashboardWorkflow).filter(DashboardWorkflow.wf_uuid == wf_uuid)
            try:
                self.wf_id_cache[wf_uuid] = query.one().wf_id
            except orm.exc.MultipleResultsFound, e:
                self.log.error('Multiple wf_id results for wf_uuid %s : %s', wf_uuid, e)
                return None
            except orm.exc.NoResultFound, e:
                self.log.error('No wf_id results for wf_uuid %s : %s', wf_uuid, e)
                return None

        return self.wf_id_cache[wf_uuid]

    def wf_uuid_to_root_id(self, wf_uuid):
        """
        @type   wf_uuid: string
        @param  wf_uuid: wf_uuid string from BP logs

        Attempts to retrieve a root workflow wf_id PK/FK from cache.  If
        not in cache, retrieve from st_workflow table in DB and cache.
        Cuts down on DB queries during insert processing.
        """
        if not self.root_wf_id_cache.has_key(wf_uuid):
            query = self.session.query(Workflow).filter(DashboardWorkflow.wf_uuid == wf_uuid)
            try:
                self.root_wf_id_cache[wf_uuid] = query.one().root_wf_id
            except orm.exc.MultipleResultsFound, e:
                self.log.error('Multiple wf_id results for wf_uuid %s : %s', wf_uuid, e)
                return None
            except orm.exc.NoResultFound, e:
                self.log.error('No wf_id results for wf_uuid %s : %s', wf_uuid, e)
                return None

        return self.root_wf_id_cache[wf_uuid]



    def flushCaches(self, wfs):
        """
        @type   wfs: class instance of stampede_schema.Workflowstate
        @param  wfs: Workflow state object from an end event.

        Flushes information from the lookup caches after a workflow.end
        event has been recieved.
        """
        self.log.debug('Flushing caches for: %s', wfs)

        for k,v in self.wf_id_cache.items():
            if k == wfs.wf_uuid:
                del self.wf_id_cache[k]

        for k,v in self.root_wf_id_cache.items():
            if k == wfs.wf_uuid:
                del self.root_wf_id_cache[k]



    ################
    # Cleanup, etc
    ################

    def finish(self):
        BaseAnalyzer.finish(self)
        if self._batch:
            self.log.info('Executing final flush')
            self.hard_flush()
        self.disconnect()
        if self._perf:
            run_time = time.time() - self._start_time
            self.log.info("Loader performance: insert_time=%s, insert_num=%s, "
                          "total_time=%s, run_time_delta=%s, mean_time=%s",
                          self._insert_time, self._insert_num, run_time,
                          run_time - self._insert_time,
                          self._insert_time / self._insert_num)
