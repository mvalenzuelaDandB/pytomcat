#!/usr/bin/env python

import time, logging
from . import *
import events

def parse_warfiles(warfiles):
    return dict((f, parse_warfile(f)) for f in warfiles)

class ClusterDeployer:
    undeploy_on_error = True
    port = 8080
    poll_interval = 5
    deploy_wait_time = 30
    gc_wait_time = 30
    required_memory = 50
    check_memory = True
    auto_gc = True
    kill_sessions = False
    auto_reboot = False

    def __init__(self, opts):
        self.log = logging.getLogger('pytomcat.deployer')
        for k, v in opts.items():
            setattr(self, k, v)
        self.c = TomcatCluster(self.host, self.user, self.passwd, self.port)
        self.c.set_progress_callback(self._progress_callback)

    def _get_webapps(self, vhost='*'):
        stats = self.c.webapp_status('*', vhost)
        self.log.debug("Received cluster-wide application status: %s", stats)
        all_paths = {}
        paths = {}
        for a, d in stats.items():
            for k, v in d['clusterDetails']['path'].items():
                all_paths.setdefault(v, []).append(k)
            paths.setdefault(d['path'], []).append(a)
        return (stats, paths, all_paths)

    def _undeploy_old_versions(self, path, apps, vhost):
        if self.kill_sessions:
            for app in apps:
                self.log.info('Forcefully expiring sessions for %s', app)
                self.c.run_command('expire_sessions', app, vhost)
        self.log.info('Attempting to undeploy old versions across the cluster')
        self.c.run_command('undeploy_old_versions', vhost)
        (stats, paths, all_paths) = self._get_webapps(vhost)
        if len(paths[path]) > 1:
            raise TomcatError(
                      "Path '{0}' is served by more than one version ({1})"
                      .format(path, ' and '.join(paths[path])))
        self.log.info('Old versions successfully undeployed')

    def _clean_old_apps(self, new_apps, vhost='*'):
        (stats, paths, all_paths) = self._get_webapps(vhost)
        for ctx, path, ver in new_apps.values():
            if ctx in stats:
                raise TomcatError(
                        'There is already a context {0} on {1}'
                        .format(ctx, ' and '.join(stats[ctx]['presentOn'])))
            if path in all_paths:
                if ver == None:
                    raise TomcatError(
                        'There is already a webapp deployed to {0} on {1}'
                        .format(path, ' and '.join(paths[path])))
                elif path not in paths:
                    raise TomcatError(
                        'Webapp {0} is deployed only to a subset of nodes ({1})'
                        .format(path, ' and '.join(paths[path])))
                else:
                    if len(paths[path]) > 1:
                        # Tomcat uses a simple sorted list of strings to determine
                        # which version is the latest
                        latest = sorted([ ctx ] + paths[path])[-1]
                        if ctx != latest:
                            raise TomcatError(
                                'There is a webapp {0} deployed to {1} that is newer than {2}'
                                .format(latest, path, ctx))
                        oldapps = sorted(paths[path])[:-1]
                        self._undeploy_old_versions(path, oldapps, vhost)

    def _get_memory(self, percentage, hosts=None):
        def ignore_filter(lst):
            ignore_pools = [ 'Par Eden Space', 'Par Survivor Space' ]
            return filter(lambda x: x not in ignore_pools, lst)
        if hosts != None:
            opts = { 'hosts': hosts }
        else:
            opts = {}
        rv = self.c.run_command('find_pools_over', 100 - self.required_memory, **opts)
        rv = dict(filter(lambda (k, v): len(ignore_filter(v)) > 0, rv.items()))
        self.log.debug('Hosts with low memory returned: %s', rv)
        return rv

    def _perform_gc(self, hosts):
        self.log.info("Running GC on the following nodes: %s", ', '.join(hosts))
        return self.c.run_command('run_gc', hosts=hosts)

    def _wait_for_free_mem(self, hosts, percentage):
        self.log.info("Waiting %ss for memory to become available", self.gc_wait_time)
        wait_total = 0
        while len(self._get_memory(percentage, hosts)) > 0:
            if wait_total > self.gc_wait_time:
                return False
            time.sleep(self.poll_interval)
            wait_total += self.poll_interval
        return True

    def _check_memory(self):
        self.log.info("Checking that all cluster nodes have at least %s%% of free memory",
                      self.required_memory)
        percentage = 100 - self.required_memory
        mem = self._get_memory(percentage)
        if len(mem) <= 0:
            return True

        hosts = mem.keys()
        errstr = 'The following nodes do not have enough memory: {0}'.format(hosts)
        self.log.info(errstr)
        if self.auto_gc:
            self._perform_gc(mem.keys())
            if self._wait_for_free_mem(hosts, percentage):
                return True
            else:
                self.log.error("Unable to reclaim memory by running GC")
        # TODO: attempt to reboot nodes to reclaim memory if instructed to do so
        self.log.error(errstr)
        raise TomcatError(errstr)

    def _wait_for_apps(self, new_apps, vhost='*'):
        ctx_list = [ ctx for ctx, path, ver in new_apps.values() ]
        wait_total = 0
        self.log.info("Waiting %ss for webapps to become available on all nodes",
                      self.deploy_wait_time)
        while wait_total < self.deploy_wait_time:
            cluster_ok = True
            stats = self.c.webapp_status('*', vhost)
            failed_apps = []
            for ctx in ctx_list:
                try:
                    cs = stats[ctx]
                    self.log.info("\t%s - %s", ctx, cs['clusterDetails']['stateName'])
                    if cs['coherent'] == False or cs['stateName'] != 'STARTED':
                        cluster_ok = False
                        failed_apps.append(ctx)
                except KeyError:
                        cluster_ok = False
                        failed_apps.append(ctx)
            if cluster_ok == True:
                break
            wait_total += self.poll_interval
            time.sleep(self.poll_interval)
        return failed_apps

    def _progress_callback(self, **args):
        handlers = {
            events.UPLOAD      : self._log_upload_status,
            events.CMD_START   : self._log_cmd_status,
            events.CMD_END     : self._log_cmd_status
        }
        event = None
        if 'event' in args:
            event=args['event']
        if event in handlers:
            handlers[event](args)

    def _log_upload_status(self, evnt):
        self.log.debug("Received upload progress event: %s", evnt)
        if evnt['position'] == 0:
            self.log.info('Starting to upload %s to %s', evnt['filename'], evnt['url'])
        elif evnt['position'] == evnt['total']:
            self.log.info('Completed uploading %s to %s', evnt['filename'], evnt['url'])

    def _log_cmd_status(self, evnt):
        msg = { events.CMD_START: {
                     'deploy'  : 'Attempting to deploy %s to %s',
                     'undeploy': 'Attempting to undeploy %s from %s' },
                 events.CMD_END: {
                     'deploy'  : 'Successfully deployed %s to %s',
                     'undeploy': 'Successfully undeployed %s from %s' }
        }
        ec = evnt['event']
        cmd = evnt['command']
        if ec in msg and cmd in msg[ec]:
            self.log.info(msg[ec][cmd], evnt['args'][0], evnt['node'])

    def _deploy(self, new_apps, vhost='localhost'):
        rv = {}
        for fn, (ctx, path, ver) in new_apps.items():
            self.log.info("Performing a cluster-wide deploy of %s", ctx)
            rv[ctx] = self.c.run_command('deploy', fn, ctx, vhost)
        return rv

    def deploy(self, new_apps, vhost='localhost'):
        '''
        Perform a cluster-wide deployment of a webapp
        Before deployment, the following tasks will be executed:
          - check that the path will not conflict with any other app in cluster
          - if the app is versioned, expire old versions before proceeding
          - check that there is enough memory available on every node
          - optionally reboot nodes to reclaim memory

        >>> from tomcat.deployer import parse_warfiles
        >>> d.deploy(parse_warfiles([ '/tmp/test.war' ]))
        '''
        self._clean_old_apps(new_apps, vhost)
        if self.check_memory:
            self._check_memory()
        rv = self._deploy(new_apps, vhost)
        self.log.debug("Deployment results %s", rv)
        # TODO: Check the results for errors
        failed = self._wait_for_apps(new_apps, vhost)
        if len(failed) > 0:
            errstr = "Deployment of {0} failed".format(' and '.join(failed))
            self.log.error(errstr)
            if self.undeploy_on_error:
                ctx_names = [ ctx for ctx, path, ver in new_apps.values() ]
                rv = self.undeploy(ctx_names, vhost)
            raise TomcatError(errstr)

    def undeploy(self, context_names, vhost='localhost'):
        '''
        Perform a cluster-wide undeploy of specified contexts

        >>> d.undeploy([ '/test1', '/test2' ])
        '''
        rv = {}
        for ctx in context_names:
            self.log.info("Performing a cluster-wide undeploy of %s", ctx)
            rv[ctx] = self.c.run_command('undeploy', ctx, vhost)
        return rv

