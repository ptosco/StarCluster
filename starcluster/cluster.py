import os
import re
import time
import string
import pprint
import warnings

from starcluster import utils
from starcluster import static
from starcluster import spinner
from starcluster import iptools
from starcluster import sshutils
from starcluster import managers
from starcluster import userdata
from starcluster import deathrow
from starcluster import exception
from starcluster import threadpool
from starcluster import validators
from starcluster import progressbar
from starcluster import clustersetup
from starcluster.node import Node
from starcluster.plugins import sge
from starcluster.utils import print_timing
from starcluster.templates import user_msgs
from starcluster.logger import log


class ClusterManager(managers.Manager):
    """
    Manager class for Cluster objects
    """
    def __repr__(self):
        return "<ClusterManager: %s>" % self.ec2.region.name

    def get_cluster(self, cluster_name, group=None, load_receipt=True,
                    load_plugins=True, load_volumes=True, require_keys=True):
        """
        Returns a Cluster object representing an active cluster
        """
        try:
            clname = self._get_cluster_name(cluster_name)
            cltag = self.get_tag_from_sg(clname)
            if not group:
                group = self.ec2.get_security_group(clname)
            cl = Cluster(ec2_conn=self.ec2, cluster_tag=cltag,
                         cluster_group=group)
            if load_receipt:
                cl.load_receipt(load_plugins=load_plugins,
                                load_volumes=load_volumes)
            try:
                cl.keyname = cl.keyname or cl.master_node.key_name
                key_location = self.cfg.get_key(cl.keyname).get('key_location')
                cl.key_location = key_location
                if require_keys:
                    cl.validator.validate_keypair()
            except (exception.KeyNotFound, exception.MasterDoesNotExist):
                if require_keys:
                    raise
                cl.key_location = ''
            return cl
        except exception.SecurityGroupDoesNotExist:
            raise exception.ClusterDoesNotExist(cluster_name)

    def get_clusters(self, load_receipt=True, load_plugins=True):
        """
        Returns a list of all active clusters
        """
        cluster_groups = self.get_cluster_security_groups()
        clusters = [self.get_cluster(g.name, group=g,
                                     load_receipt=load_receipt,
                                     load_plugins=load_plugins)
                    for g in cluster_groups]
        return clusters

    def get_default_cluster_template(self):
        """
        Returns name of the default cluster template defined in the config
        """
        return self.cfg.get_default_cluster_template()

    def get_cluster_template(self, template_name, tag_name=None):
        """
        Returns a new Cluster object using the settings from the cluster
        template template_name

        If tag_name is passed, the Cluster object's cluster_tag setting will
        be set to tag_name
        """
        cl = self.cfg.get_cluster_template(template_name, tag_name=tag_name,
                                           ec2_conn=self.ec2)
        return cl

    def get_cluster_or_none(self, cluster_name, **kwargs):
        """
        Same as get_cluster but returns None instead of throwing an exception
        if the cluster does not exist
        """
        try:
            return self.get_cluster(cluster_name, **kwargs)
        except exception.ClusterDoesNotExist:
            pass

    def cluster_exists(self, tag_name):
        """
        Returns True if cluster exists
        """
        return self.get_cluster_or_none(tag_name) is not None

    def ssh_to_master(self, cluster_name, user='root', command=None,
                      forward_x11=False, forward_agent=False):
        """
        ssh to master node of cluster_name

        user keyword specifies an alternate user to login as
        """
        cluster = self.get_cluster(cluster_name, load_receipt=False,
                                   require_keys=True)
        return cluster.ssh_to_master(user=user, command=command,
                                     forward_x11=forward_x11,
                                     forward_agent=forward_agent)

    def ssh_to_cluster_node(self, cluster_name, node_id, user='root',
                            command=None, forward_x11=False,
                            forward_agent=False):
        """
        ssh to a node in cluster_name that has either an id,
        dns name, or alias matching node_id

        user keyword specifies an alternate user to login as
        """
        cluster = self.get_cluster(cluster_name, load_receipt=False,
                                   require_keys=False)
        node = cluster.get_node_by_alias(node_id)
        key_location = self.cfg.get_key(node.key_name).get('key_location')
        cluster.key_location = key_location
        cluster.validator.validate_keypair()
        return node.shell(user=user, forward_x11=forward_x11,
                          forward_agent=forward_agent, command=command)

    def _get_cluster_name(self, cluster_name):
        """
        Returns human readable cluster name/tag prefixed with '@sc-'
        """
        if not cluster_name.startswith(static.SECURITY_GROUP_PREFIX):
            cluster_name = static.SECURITY_GROUP_TEMPLATE % cluster_name
        return cluster_name

    def add_node(self, cluster_name, alias=None, no_create=False,
                 image_id=None, instance_type=None, zone=None,
                 placement_group=None, spot_bid=None):
        cl = self.get_cluster(cluster_name)
        return cl.add_node(alias=alias, image_id=image_id,
                           instance_type=instance_type, zone=zone,
                           placement_group=placement_group, spot_bid=spot_bid,
                           no_create=no_create)

    def add_nodes(self, cluster_name, num_nodes, aliases=None, no_create=False,
                  image_id=None, instance_type=None, zone=None,
                  placement_group=None, spot_bid=None):
        """
        Add one or more nodes to cluster
        """
        cl = self.get_cluster(cluster_name)
        return cl.add_nodes(num_nodes, aliases=aliases, image_id=image_id,
                            instance_type=instance_type, zone=zone,
                            placement_group=placement_group, spot_bid=spot_bid,
                            no_create=no_create)

    def remove_node(self, cluster_name, alias, terminate=True):
        """
        Remove a single node from a cluster
        """
        cl = self.get_cluster(cluster_name)
        n = cl.get_node_by_alias(alias)
        if not n:
            raise exception.InstanceDoesNotExist(alias, label='node')
        cl.remove_node(n, terminate=terminate)

    def restart_cluster(self, cluster_name):
        """
        Reboots and reconfigures cluster_name
        """
        cl = self.get_cluster(cluster_name)
        cl.restart_cluster()

    def stop_cluster(self, cluster_name, terminate_unstoppable=False,
                     force=False):
        """
        Stop an EBS-backed cluster
        """
        cl = self.get_cluster(cluster_name, load_receipt=not force,
                              require_keys=not force)
        cl.stop_cluster(terminate_unstoppable, force=force)

    def terminate_cluster(self, cluster_name, force=False):
        """
        Terminates cluster_name
        """
        cl = self.get_cluster(cluster_name, load_receipt=not force,
                              require_keys=not force)
        cl.terminate_cluster(force=force)

    def get_cluster_security_group(self, group_name):
        """
        Return cluster security group by appending '@sc-' to group_name and
        querying EC2.
        """
        gname = self._get_cluster_name(group_name)
        return self.ec2.get_security_group(gname)

    def get_cluster_group_or_none(self, group_name):
        try:
            return self.get_cluster_security_group(group_name)
        except exception.SecurityGroupDoesNotExist:
            pass

    def get_cluster_security_groups(self):
        """
        Return all security groups on EC2 that start with '@sc-'
        """
        glob = static.SECURITY_GROUP_TEMPLATE % '*'
        sgs = self.ec2.get_security_groups(filters={'group-name': glob})
        return sgs

    def get_tag_from_sg(self, sg):
        """
        Returns the cluster tag name from a security group name that starts
        with static.SECURITY_GROUP_PREFIX

        Example:
            sg = '@sc-mycluster'
            print get_tag_from_sg(sg)
            mycluster
        """
        regex = re.compile('^' + static.SECURITY_GROUP_PREFIX + '-(.*)')
        match = regex.match(sg)
        tag = None
        if match:
            tag = match.groups()[0]
        if not tag:
            raise ValueError("Invalid cluster group name: %s" % sg)
        return tag

    def list_clusters(self, cluster_groups=None, show_ssh_status=False):
        """
        Prints a summary for each active cluster on EC2
        """
        if not cluster_groups:
            cluster_groups = self.get_cluster_security_groups()
            if not cluster_groups:
                log.info("No clusters found...")
        else:
            try:
                cluster_groups = [self.get_cluster_security_group(g) for g
                                  in cluster_groups]
            except exception.SecurityGroupDoesNotExist:
                raise exception.ClusterDoesNotExist(g)
        for scg in cluster_groups:
            tag = self.get_tag_from_sg(scg.name)
            try:
                cl = self.get_cluster(tag, group=scg, load_plugins=False,
                                      load_volumes=False, require_keys=False)
            except exception.IncompatibleCluster, e:
                sep = '*' * 60
                log.error('\n'.join([sep, e.msg, sep]),
                          extra=dict(__textwrap__=True))
                print
                continue
            header = '%s (security group: %s)' % (tag, scg.name)
            print '-' * len(header)
            print header
            print '-' * len(header)
            nodes = cl.nodes
            try:
                n = nodes[0]
            except IndexError:
                n = None
            state = getattr(n, 'state', None)
            ltime = 'N/A'
            uptime = 'N/A'
            if state in ['pending', 'running']:
                ltime = getattr(n, 'local_launch_time', 'N/A')
                uptime = getattr(n, 'uptime', 'N/A')
            print 'Launch time: %s' % ltime
            print 'Uptime: %s' % uptime
            print 'Zone: %s' % getattr(n, 'placement', 'N/A')
            print 'Keypair: %s' % getattr(n, 'key_name', 'N/A')
            ebs_nodes = [n for n in nodes if n.attached_vols]
            if ebs_nodes:
                print 'EBS volumes:'
                for node in ebs_nodes:
                    devices = node.attached_vols
                    node_id = node.alias or node.id
                    for dev in devices:
                        d = devices.get(dev)
                        vol_id = d.volume_id
                        status = d.status
                        print('    %s on %s:%s (status: %s)' %
                              (vol_id, node_id, dev, status))
            else:
                print 'EBS volumes: N/A'
            spot_reqs = cl.spot_requests
            if spot_reqs:
                active = len([s for s in spot_reqs if s.state == 'active'])
                opn = len([s for s in spot_reqs if s.state == 'open'])
                msg = ''
                if active != 0:
                    msg += '%d active' % active
                if opn != 0:
                    if msg:
                        msg += ', '
                    msg += '%d open' % opn
                print 'Spot requests: %s' % msg
            if nodes:
                print 'Cluster nodes:'
                for node in nodes:
                    nodeline = "    %7s %s %s %s" % (node.alias, node.state,
                                                     node.id, node.dns_name)
                    if node.spot_id:
                        nodeline += ' (spot %s)' % node.spot_id
                    if show_ssh_status:
                        ssh_status = {True: 'Up', False: 'Down'}
                        nodeline += ' (SSH: %s)' % ssh_status[node.is_up()]
                    print nodeline
                print 'Total nodes: %d' % len(nodes)
            else:
                print 'Cluster nodes: N/A'
            print

    def run_plugin(self, plugin_name, cluster_tag):
        """
        Run a plugin defined in the config.

        plugin_name must match the plugin's section name in the config
        cluster_tag specifies the cluster to run the plugin on
        """
        cl = self.get_cluster(cluster_tag, load_plugins=False)
        if not cl.is_cluster_up():
            raise exception.ClusterNotRunning(cluster_tag)
        plugs = [self.cfg.get_plugin(plugin_name)]
        name, plugin = cl.load_plugins(plugs)[0]
        cl.run_plugin(plugin, name)


class Cluster(object):
    def __init__(self,
                 ec2_conn=None,
                 spot_bid=None,
                 cluster_tag=None,
                 cluster_description=None,
                 cluster_size=None,
                 cluster_user=None,
                 cluster_shell=None,
                 master_image_id=None,
                 master_instance_type=None,
                 node_image_id=None,
                 node_instance_type=None,
                 node_instance_types=[],
                 availability_zone=None,
                 keyname=None,
                 key_location=None,
                 volumes=[],
                 plugins=[],
                 permissions=[],
                 userdata_scripts=[],
                 refresh_interval=30,
                 disable_queue=False,
                 num_threads=20,
                 disable_threads=False,
                 cluster_group=None,
                 force_spot_master=False,
                 disable_cloudinit=False,
                 **kwargs):

        now = time.strftime("%Y%m%d%H%M")
        self.ec2 = ec2_conn
        self.spot_bid = spot_bid
        self.cluster_tag = cluster_tag
        self.cluster_description = cluster_description
        if self.cluster_tag is None:
            self.cluster_tag = "cluster%s" % now
        if cluster_description is None:
            self.cluster_description = "Cluster created at %s" % now
        self.cluster_size = cluster_size or 0
        self.cluster_user = cluster_user
        self.cluster_shell = cluster_shell
        self.master_image_id = master_image_id
        self.master_instance_type = master_instance_type
        self.node_image_id = node_image_id
        self.node_instance_type = node_instance_type
        self.node_instance_types = node_instance_types
        self.availability_zone = availability_zone
        self.keyname = keyname
        self.key_location = key_location
        self.volumes = self.load_volumes(volumes)
        self.plugins = self.load_plugins(plugins)
        self.permissions = permissions
        self.userdata_scripts = userdata_scripts or []
        self.refresh_interval = refresh_interval
        self.disable_queue = disable_queue
        self.num_threads = num_threads
        self.disable_threads = disable_threads
        self.force_spot_master = force_spot_master
        self.disable_cloudinit = disable_cloudinit

        self._cluster_group = None
        self._placement_group = None
        self._zone = None
        self._master = None
        self._nodes = []
        self._pool = None
        self._progress_bar = None

    def __repr__(self):
        return '<Cluster: %s (%s-node)>' % (self.cluster_tag,
                                            self.cluster_size)

    @property
    def zone(self):
        if not self._zone and self.availability_zone or self.volumes:
            self._zone = self._get_cluster_zone()
        return self._zone

    def _get_cluster_zone(self):
        """
        Returns the cluster's zone. If volumes are specified, this method
        determines the common zone between those volumes. If a zone is
        explicitly specified in the config and does not match the common zone
        of the volumes, an exception is raised. If all volumes are not in the
        same zone an exception is raised. If no volumes are specified, returns
        the user-specified zone if it exists. Returns None if no volumes and no
        zone is specified.
        """
        zone = None
        if self.availability_zone:
            zone = self.ec2.get_zone(self.availability_zone)
        common_zone = None
        for volume in self.volumes:
            volid = self.volumes.get(volume).get('volume_id')
            vol = self.ec2.get_volume(volid)
            if not common_zone:
                common_zone = vol.zone
            elif vol.zone != common_zone:
                vols = [self.volumes.get(v).get('volume_id')
                        for v in self.volumes]
                raise exception.VolumesZoneError(vols)
        if common_zone and zone and zone.name != common_zone:
            raise exception.InvalidZone(zone.name, common_zone)
        if not zone and common_zone:
            zone = self.ec2.get_zone(common_zone)
        return zone

    @property
    def _plugins(self):
        return [p.__plugin_metadata__ for p in self.plugins]

    def load_plugins(self, plugins):
        if plugins and isinstance(plugins[0], dict):
            warnings.warn("In a future release the plugins kwarg for Cluster "
                          "will require a list of plugin objects and not a "
                          "list of dicts", DeprecationWarning)
            plugins = deathrow._load_plugins(plugins)
        return plugins

    def load_volumes(self, vols):
        """
        Iterate through vols and set device/partition settings automatically if
        not specified.

        This method assigns the first volume to /dev/sdz, second to /dev/sdy,
        etc. for all volumes that do not include a device/partition setting
        """
        devices = ['/dev/sd%s' % s for s in string.lowercase]
        devmap = {}
        for volname in vols:
            vol = vols.get(volname)
            dev = vol.get('device')
            if dev in devices:
                #rm user-defined devices from the list of auto-assigned devices
                devices.remove(dev)
            volid = vol.get('volume_id')
            if dev and not volid in devmap:
                devmap[volid] = dev
        volumes = {}
        for volname in vols:
            vol = vols.get(volname)
            vol_id = vol.get('volume_id')
            device = vol.get('device')
            if not device:
                if vol_id in devmap:
                    device = devmap.get(vol_id)
                else:
                    device = devices.pop()
                    devmap[vol_id] = device
            if not utils.is_valid_device(device):
                raise exception.InvalidDevice(device)
            v = volumes[volname] = utils.AttributeDict()
            v.update(vol)
            v['device'] = device
            part = vol.get('partition')
            if part:
                partition = device + str(part)
                if not utils.is_valid_partition(partition):
                    raise exception.InvalidPartition(part)
                v['partition'] = partition
        return volumes

    def update(self, kwargs):
        for key in kwargs.keys():
            if hasattr(self, key):
                self.__dict__[key] = kwargs[key]

    def get(self, name):
        return self.__dict__.get(name)

    def __str__(self):
        cfg = self.__getstate__()
        return pprint.pformat(cfg)

    def load_receipt(self, load_plugins=True, load_volumes=True):
        """
        Load the original settings used to launch this cluster into this
        Cluster object. Settings are loaded from cluster group tags and the
        master node's user data.
        """
        try:
            tags = self.cluster_group.tags
            version = tags.get(static.VERSION_TAG, '')
            if utils.program_version_greater(version, static.VERSION):
                d = dict(cluster=self.cluster_tag, old_version=static.VERSION,
                         new_version=version)
                msg = user_msgs.version_mismatch % d
                sep = '*' * 60
                log.warn('\n'.join([sep, msg, sep]), extra={'__textwrap__': 1})
            cluster_settings = {}
            if static.CORE_TAG in tags:
                core = tags.get(static.CORE_TAG, '')
                cluster_settings.update(
                    utils.decode_uncompress_load(core, use_json=True))
            if static.USER_TAG in tags:
                user = tags.get(static.USER_TAG, '')
                cluster_settings.update(
                    utils.decode_uncompress_load(user, use_json=True))
            self.update(cluster_settings)
            if not (load_plugins or load_volumes):
                return True
            try:
                master = self.master_node
            except exception.MasterDoesNotExist:
                if self.spot_requests:
                    self.wait_for_active_spots()
                    self.wait_for_active_instances()
                    master = self.master_node
                else:
                    raise
            if load_plugins:
                self.plugins = self.load_plugins(master.get_plugins())
            if load_volumes:
                self.volumes = master.get_volumes()
        except exception.PluginError:
            log.error("An error occurred while loading plugins: ",
                      exc_info=True)
            raise
        except exception.MasterDoesNotExist:
            raise
        except Exception:
            log.debug('load receipt exception: ', exc_info=True)
            raise exception.IncompatibleCluster(self.cluster_group)
        return True

    def __getstate__(self):
        cfg = {}
        exclude = ['key_location', 'plugins']
        include = ['_zone', '_plugins']
        for key in self.__dict__.keys():
            private = key.startswith('_')
            if (not private or key in include) and not key in exclude:
                val = getattr(self, key)
                if type(val) in [str, unicode, bool, int, float, list, dict]:
                    cfg[key] = val
                elif type(val) is utils.AttributeDict:
                    cfg[key] = dict(val)
        return cfg

    @property
    def _security_group(self):
        return static.SECURITY_GROUP_TEMPLATE % self.cluster_tag

    @property
    def cluster_group(self):
        if self._cluster_group is None:
            desc = 'StarCluster-%s' % static.VERSION.replace('.', '_')
            sg = self.ec2.get_group_or_none(self._security_group)
            if not sg:
                sg = self.ec2.create_group(self._security_group,
                                           description=desc, auth_ssh=True,
                                           auth_group_traffic=True)
                if not static.VERSION_TAG in sg.tags:
                    sg.add_tag(static.VERSION_TAG, str(static.VERSION))
                core_settings = utils.dump_compress_encode(
                    dict(cluster_size=self.cluster_size,
                         master_image_id=self.master_image_id,
                         master_instance_type=self.master_instance_type,
                         node_image_id=self.node_image_id,
                         node_instance_type=self.node_instance_type,
                         disable_queue=self.disable_queue,
                         disable_cloudinit=self.disable_cloudinit),
                    use_json=True)
                if not static.CORE_TAG in sg.tags:
                    sg.add_tag('@sc-core', core_settings)
                user_settings = utils.dump_compress_encode(
                    dict(cluster_user=self.cluster_user,
                         cluster_shell=self.cluster_shell,
                         keyname=self.keyname,
                         spot_bid=self.spot_bid), use_json=True)
                if not static.USER_TAG in sg.tags:
                    sg.add_tag('@sc-user', user_settings)
            ssh_port = static.DEFAULT_SSH_PORT
            for p in self.permissions:
                perm = self.permissions.get(p)
                ip_protocol = perm.get('ip_protocol', 'tcp')
                from_port = perm.get('from_port')
                to_port = perm.get('to_port')
                cidr_ip = perm.get('cidr_ip', static.WORLD_CIDRIP)
                if not self.ec2.has_permission(sg, ip_protocol, from_port,
                                               to_port, cidr_ip):
                    log.info("Opening %s port range %s-%s for CIDR %s" %
                             (ip_protocol, from_port, to_port, cidr_ip))
                    sg.authorize(ip_protocol, from_port, to_port, cidr_ip)
                includes_ssh = from_port <= ssh_port <= to_port
                open_to_world = cidr_ip == static.WORLD_CIDRIP
                if ip_protocol == 'tcp' and includes_ssh and not open_to_world:
                    sg.revoke(ip_protocol, ssh_port, ssh_port,
                              static.WORLD_CIDRIP)
            self._cluster_group = sg
        return self._cluster_group

    @property
    def placement_group(self):
        if self._placement_group is None:
            pg = self.ec2.get_or_create_placement_group(self._security_group)
            self._placement_group = pg
        return self._placement_group

    @property
    def master_node(self):
        if not self._master:
            for node in self.nodes:
                if node.is_master():
                    self._master = node
            if not self._master:
                raise exception.MasterDoesNotExist()
        self._master.key_location = self.key_location
        return self._master

    @property
    def nodes(self):
        states = ['pending', 'running', 'stopping', 'stopped']
        filters = {'group-name': self._security_group,
                   'instance-state-name': states}
        nodes = self.ec2.get_all_instances(filters=filters)
        # remove any cached nodes not in the current node list from EC2
        current_ids = [n.id for n in nodes]
        remove_nodes = [n for n in self._nodes if n.id not in current_ids]
        for node in remove_nodes:
            self._nodes.remove(node)
        # update node cache with latest instance data from EC2
        existing_nodes = dict([(n.id, n) for n in self._nodes])
        log.debug('existing nodes: %s' % existing_nodes)
        for node in nodes:
            if node.id in existing_nodes:
                log.debug('updating existing node %s in self._nodes' % node.id)
                enode = existing_nodes.get(node.id)
                enode.key_location = self.key_location
                enode.instance = node
            else:
                log.debug('adding node %s to self._nodes list' % node.id)
                n = Node(node, self.key_location)
                if n.is_master():
                    self._master = n
                    self._nodes.insert(0, n)
                else:
                    self._nodes.append(n)
        self._nodes.sort(key=lambda n: n.alias)
        log.debug('returning self._nodes = %s' % self._nodes)
        return self._nodes

    def get_nodes_or_raise(self):
        nodes = self.nodes
        if not nodes:
            filters = {'group-name': self._security_group}
            terminated_nodes = self.ec2.get_all_instances(filters=filters)
            raise exception.NoClusterNodesFound(terminated_nodes)
        return nodes

    def get_node_by_dns_name(self, dns_name):
        for node in self.nodes:
            if node.dns_name == dns_name:
                return node
        raise exception.InstanceDoesNotExist(dns_name, label='node')

    def get_node_by_id(self, instance_id):
        for node in self.nodes:
            if node.id == instance_id:
                return node
        raise exception.InstanceDoesNotExist(instance_id, label='node')

    def get_node_by_alias(self, alias):
        for node in self.nodes:
            if node.alias == alias:
                return node
        raise exception.InstanceDoesNotExist(alias, label='node')

    def _nodes_in_states(self, states):
        return filter(lambda x: x.state in states, self.nodes)

    @property
    def running_nodes(self):
        return self._nodes_in_states(['running'])

    @property
    def stopped_nodes(self):
        return self._nodes_in_states(['stopping', 'stopped'])

    @property
    def spot_requests(self):
        filters = {'launch.group-id': self.cluster_group.id,
                   'state': ['active', 'open']}
        return self.ec2.get_all_spot_requests(filters=filters)

    def get_spot_requests_or_raise(self):
        spots = self.spot_requests
        if not spots:
            raise exception.NoClusterSpotRequests
        return spots

    def create_node(self, alias, image_id=None, instance_type=None, zone=None,
                    placement_group=None, spot_bid=None, force_flat=False):
        return self.create_nodes([alias], image_id=image_id,
                                 instance_type=instance_type, zone=zone,
                                 placement_group=placement_group,
                                 spot_bid=spot_bid, force_flat=force_flat)[0]

    def _get_cluster_userdata(self, aliases):
        alias_file = utils.string_to_file('\n'.join(['#ignored'] + aliases),
                                          static.UD_ALIASES_FNAME)
        plugins = utils.dump_compress_encode(self._plugins)
        plugins_file = utils.string_to_file('\n'.join(['#ignored', plugins]),
                                            static.UD_PLUGINS_FNAME)
        volumes = utils.dump_compress_encode(self.volumes)
        volumes_file = utils.string_to_file('\n'.join(['#ignored', volumes]),
                                            static.UD_VOLUMES_FNAME)
        udfiles = [alias_file, plugins_file, volumes_file]
        user_scripts = self.userdata_scripts or []
        udfiles += [open(f) for f in user_scripts]
        use_cloudinit = not self.disable_cloudinit
        udata = userdata.bundle_userdata_files(udfiles,
                                               use_cloudinit=use_cloudinit)
        return udata

    def create_nodes(self, aliases, image_id=None, instance_type=None,
                     zone=None, placement_group=None, spot_bid=None,
                     force_flat=False):
        """
        Convenience method for requesting instances with this cluster's
        settings. All settings (kwargs) except force_flat default to cluster
        settings if not provided. Passing force_flat=True ignores spot_bid
        completely forcing a flat-rate instance to be requested.
        """
        spot_bid = spot_bid or self.spot_bid
        if force_flat:
            spot_bid = None
        cluster_sg = self.cluster_group.name
        instance_type = instance_type or self.node_instance_type
        if not placement_group and instance_type in static.CLUSTER_TYPES:
            placement_group = self.placement_group.name
        image_id = image_id or self.node_image_id
        count = len(aliases) if not spot_bid else 1
        user_data = self._get_cluster_userdata(aliases)
        kwargs = dict(price=spot_bid, instance_type=instance_type,
                      min_count=count, max_count=count, count=count,
                      key_name=self.keyname, security_groups=[cluster_sg],
                      availability_zone_group=cluster_sg,
                      launch_group=cluster_sg,
                      placement=zone or getattr(self.zone, 'name', None),
                      user_data=user_data,
                      placement_group=placement_group)
        resvs = []
        if spot_bid:
            for alias in aliases:
                kwargs['user_data'] = self._get_cluster_userdata([alias])
                resvs.extend(self.ec2.request_instances(image_id, **kwargs))
        else:
            resvs.append(self.ec2.request_instances(image_id, **kwargs))
        for resv in resvs:
            log.info(str(resv), extra=dict(__raw__=True))
        return resvs

    def _get_next_node_num(self):
        nodes = self._nodes_in_states(['pending', 'running'])
        nodes = filter(lambda x: not x.is_master(), nodes)
        highest = 0
        for n in nodes:
            try:
                highest = max(highest, int(n.alias[4:8]))
            except ValueError:
                pass
        next = highest + 1
        log.debug("Highest node number is %d. choosing %d." % (highest, next))
        return next

    def add_node(self, alias=None, no_create=False, image_id=None,
                 instance_type=None, zone=None, placement_group=None,
                 spot_bid=None):
        """
        Add a single node to this cluster
        """
        aliases = None
        if alias:
            aliases = [alias]
        return self.add_nodes(1, aliases=aliases, image_id=image_id,
                              instance_type=instance_type, zone=zone,
                              placement_group=placement_group,
                              spot_bid=spot_bid, no_create=no_create)

    def add_nodes(self, num_nodes, aliases=None, image_id=None,
                  instance_type=None, zone=None, placement_group=None,
                  spot_bid=None, no_create=False, reboot_interval=10,
                  n_reboot_restart=False):
        """
        Add new nodes to this cluster

        aliases - list of aliases to assign to new nodes (len must equal
        num_nodes)
        """
        running_pending = self._nodes_in_states(['pending', 'running'])
        aliases = aliases or []
        if not aliases:
            next_node_id = self._get_next_node_num()
            for i in range(next_node_id, next_node_id + num_nodes):
                alias = 'node%.3d' % i
                aliases.append(alias)
        assert len(aliases) == num_nodes
        if "master" in aliases:
            raise exception.ClusterValidationError(
                "worker nodes cannot have master as an alias")
        if not no_create:
            for node in running_pending:
                if node.alias in aliases:
                    raise exception.ClusterValidationError(
                        "node with alias %s already exists" % node.alias)
            log.info("Launching node(s): %s" % ', '.join(aliases))
            self.create_nodes(aliases, image_id=image_id,
                              instance_type=instance_type, zone=zone,
                              placement_group=placement_group,
                              spot_bid=spot_bid)
        self.wait_for_cluster(msg="Waiting for node(s) to come up...",
                              reboot_interval=reboot_interval,
                              n_reboot_restart=n_reboot_restart)
        if all([not no_create, spot_bid, reboot_interval, n_reboot_restart]):
            #this will recreate the spot instances that might have died in
            #wait_for_cluster
            try_again_aliases = []
            while 1:
                for alias in aliases:
                    #verify all nodes were correctly added
                    try:
                        self.get_node_by_alias(alias)
                    except exception.InstanceDoesNotExist:
                        try_again_aliases.append(alias)
                if try_again_aliases:
                    log.info("Some spot instances have been terminated and "
                             "will be recreated.")
                    self.create_nodes(try_again_aliases, image_id=image_id,
                                      instance_type=instance_type, zone=zone,
                                      placement_group=placement_group,
                                      spot_bid=spot_bid)
                    self.wait_for_cluster(
                        msg="Waiting for node(s) to come up...",
                        reboot_interval=reboot_interval,
                        n_reboot_restart=n_reboot_restart)
                else:
                    #all nodes successfully created
                    break

        log.debug("Adding node(s): %s" % aliases)
        default_plugin = clustersetup.DefaultClusterSetup(
            disable_threads=self.disable_threads, num_threads=self.num_threads)
        if not self.disable_queue:
            sge_plugin = sge.SGEPlugin(disable_threads=self.disable_threads,
                                       num_threads=self.num_threads)
        for alias in aliases:
            node = self.get_node_by_alias(alias)
            default_plugin.on_add_node(node, self.nodes, self.master_node,
                                       self.cluster_user, self.cluster_shell,
                                       self.volumes)
            if not self.disable_queue:
                sge_plugin.on_add_node(node, self.nodes, self.master_node,
                                       self.cluster_user, self.cluster_shell,
                                       self.volumes)
            self.run_plugins(method_name="on_add_node", node=node)

    def remove_node(self, node, terminate=True):
        """
        Remove a single node from this cluster
        """
        return self.remove_nodes([node], terminate=terminate)

    def remove_nodes(self, nodes, terminate=True):
        """
        Remove a list of nodes from this cluster
        """
        default_plugin = clustersetup.DefaultClusterSetup(
            disable_threads=self.disable_threads, num_threads=self.num_threads)
        if not self.disable_queue:
            sge_plugin = sge.SGEPlugin(disable_threads=self.disable_threads,
                                       num_threads=self.num_threads)
        for node in nodes:
            if node.is_master():
                raise exception.InvalidOperation("cannot remove master node")
            self.run_plugins(method_name="on_remove_node",
                             node=node, reverse=True)
            if not self.disable_queue:
                sge_plugin.on_remove_node(node, self.nodes, self.master_node,
                                          self.cluster_user,
                                          self.cluster_shell, self.volumes)
            default_plugin.on_remove_node(node, self.nodes, self.master_node,
                                          self.cluster_user,
                                          self.cluster_shell, self.volumes)
            if not terminate:
                continue
            if node.spot_id:
                log.info("Canceling spot request %s" % node.spot_id)
                node.get_spot_request().cancel()
            node.terminate()

    def _get_launch_map(self, reverse=False):
        """
        Groups all node-aliases that have similar instance types/image ids
        Returns a dictionary that's used to launch all similar instance types
        and image ids in the same request. Example return value:

        {('c1.xlarge', 'ami-a5c02dcc'): ['node001', 'node002'],
         ('m1.large', 'ami-a5c02dcc'): ['node003'],
         ('m1.small', 'ami-17b15e7e'): ['master', 'node005', 'node006'],
         ('m1.small', 'ami-19e17a2b'): ['node004']}

        Passing reverse=True will return the same information only keyed by
        node aliases:

        {'master': ('m1.small', 'ami-17b15e7e'),
         'node001': ('c1.xlarge', 'ami-a5c02dcc'),
         'node002': ('c1.xlarge', 'ami-a5c02dcc'),
         'node003': ('m1.large', 'ami-a5c02dcc'),
         'node004': ('m1.small', 'ami-19e17a2b'),
         'node005': ('m1.small', 'ami-17b15e7e'),
         'node006': ('m1.small', 'ami-17b15e7e')}
        """
        lmap = {}
        mtype = self.master_instance_type or self.node_instance_type
        mimage = self.master_image_id or self.node_image_id
        lmap[(mtype, mimage)] = ['master']
        id_start = 1
        for itype in self.node_instance_types:
            count = itype['size']
            image_id = itype['image'] or self.node_image_id
            type = itype['type'] or self.node_instance_type
            if not (type, image_id) in lmap:
                lmap[(type, image_id)] = []
            for id in range(id_start, id_start + count):
                alias = 'node%.3d' % id
                log.debug("Launch map: %s (ami: %s, type: %s)..." %
                          (alias, image_id, type))
                lmap[(type, image_id)].append(alias)
                id_start += 1
        ntype = self.node_instance_type
        nimage = self.node_image_id
        if not (ntype, nimage) in lmap:
            lmap[(ntype, nimage)] = []
        for id in range(id_start, self.cluster_size):
            alias = 'node%.3d' % id
            log.debug("Launch map: %s (ami: %s, type: %s)..." %
                      (alias, nimage, ntype))
            lmap[(ntype, nimage)].append(alias)
        if reverse:
            rlmap = {}
            for (itype, image_id) in lmap:
                aliases = lmap.get((itype, image_id))
                for alias in aliases:
                    rlmap[alias] = (itype, image_id)
            return rlmap
        return lmap

    def _get_type_and_image_id(self, alias):
        """
        Returns (instance_type,image_id) for a given alias based
        on the map returned from self._get_launch_map
        """
        lmap = self._get_launch_map()
        for (type, image) in lmap:
            key = (type, image)
            if alias in lmap.get(key):
                return key

    def create_cluster(self):
        """
        Launches all EC2 instances based on this cluster's settings.
        """
        log.info("Launching a %d-node cluster..." % self.cluster_size)
        mtype = self.master_instance_type or self.node_instance_type
        self.master_instance_type = mtype
        if self.spot_bid:
            self._create_spot_cluster()
        else:
            self._create_flat_rate_cluster()

    def _create_flat_rate_cluster(self):
        """
        Launches cluster using flat-rate instances. This method attempts to
        minimize the number of launch requests by grouping nodes of the same
        type/ami and launching each group simultaneously within a single launch
        request. This is especially important for Cluster Compute instances
        given that Amazon *highly* recommends requesting all CCI in a single
        launch request.
        """
        lmap = self._get_launch_map()
        zone = None
        master_map = None
        for (type, image) in lmap:
            # launch all aliases that match master's itype/image_id
            aliases = lmap.get((type, image))
            if 'master' in aliases:
                master_map = (type, image)
                for alias in aliases:
                    log.debug("Launching %s (ami: %s, type: %s)" %
                              (alias, image, type))
                master_response = self.create_nodes(aliases, image_id=image,
                                                    instance_type=type,
                                                    force_flat=True)[0]
                zone = master_response.instances[0].placement
        lmap.pop(master_map)
        if self.cluster_size <= 1:
            return
        for (type, image) in lmap:
            aliases = lmap.get((type, image))
            for alias in aliases:
                log.debug("Launching %s (ami: %s, type: %s)" %
                          (alias, image, type))
            self.create_nodes(aliases, image_id=image, instance_type=type,
                              zone=zone, force_flat=True)

    def _create_spot_cluster(self):
        """
        Launches cluster using spot instances for all worker nodes. This method
        makes a single spot request for each node in the cluster since spot
        instances *always* have an ami_launch_index of 0. This is needed in
        order to correctly assign aliases to nodes.
        """
        (mtype, mimage) = self._get_type_and_image_id('master')
        log.info("Launching master node (ami: %s, type: %s)..." %
                 (mimage, mtype))
        force_flat = not self.force_spot_master
        master_response = self.create_node('master',
                                           image_id=mimage,
                                           instance_type=mtype,
                                           force_flat=force_flat)
        zone = None
        if not force_flat and self.spot_bid:
            # Make sure nodes are in same zone as master
            launch_spec = master_response.launch_specification
            zone = launch_spec.placement
        else:
            # Make sure nodes are in same zone as master
            zone = master_response.instances[0].placement
        if self.cluster_size <= 1:
            return
        for id in range(1, self.cluster_size):
            alias = 'node%.3d' % id
            (ntype, nimage) = self._get_type_and_image_id(alias)
            log.info("Launching %s (ami: %s, type: %s)" %
                     (alias, nimage, ntype))
            self.create_node(alias, image_id=nimage, instance_type=ntype,
                             zone=zone)

    def is_spot_cluster(self):
        """
        Returns True if all nodes are spot instances
        """
        nodes = self.nodes
        if not nodes:
            return False
        for node in nodes:
            if not node.is_spot():
                return False
        return True

    def has_spot_nodes(self):
        """
        Returns True if any nodes are spot instances
        """
        for node in self.nodes:
            if node.is_spot():
                return True
        return False

    def is_ebs_cluster(self):
        """
        Returns True if all nodes are EBS-backed
        """
        nodes = self.nodes
        if not nodes:
            return False
        for node in nodes:
            if not node.is_ebs_backed():
                return False
        return True

    def has_ebs_nodes(self):
        """
        Returns True if any nodes are EBS-backed
        """
        for node in self.nodes:
            if node.is_ebs_backed():
                return True
        return False

    def is_stoppable(self):
        """
        Returns True if all nodes are stoppable (i.e. non-spot and EBS-backed)
        """
        nodes = self.nodes
        if not nodes:
            return False
        for node in self.nodes:
            if not node.is_stoppable():
                return False
        return True

    def has_stoppable_nodes(self):
        """
        Returns True if any nodes are stoppable (i.e. non-spot and EBS-backed)
        """
        nodes = self.nodes
        if not nodes:
            return False
        for node in nodes:
            if node.is_stoppable():
                return True
        return False

    def is_cluster_compute(self):
        """
        Returns true if all instances are Cluster/GPU Compute type
        """
        nodes = self.nodes
        if not nodes:
            return False
        for node in nodes:
            if not node.is_cluster_compute():
                return False
        return True

    def has_cluster_compute_nodes(self):
        for node in self.nodes:
            if node.is_cluster_compute():
                return True
        return False

    def is_cluster_up(self):
        """
        Check that all nodes are 'running' and that ssh is up on all nodes
        This method will return False if any spot requests are in an 'open'
        state.
        """
        spots = self.spot_requests
        active_spots = filter(lambda x: x.state == 'active', spots)
        if len(spots) != len(active_spots):
            return False
        nodes = self.nodes
        if not nodes:
            return False
        for node in nodes:
            if not node.is_up():
                return False
        return True

    def get_spinner(self, msg):
        """
        Logs a status msg, starts a spinner, and returns the spinner object.
        This is useful for long running processes:

            s = self.get_spinner("Long running process running...")
            (do something)
            s.stop()
        """
        s = spinner.Spinner()
        log.info(msg, extra=dict(__nonewline__=True))
        s.start()
        return s

    @property
    def progress_bar(self):
        if not self._progress_bar:
            widgets = ['', progressbar.Fraction(), ' ',
                       progressbar.Bar(marker=progressbar.RotatingMarker()),
                       ' ', progressbar.Percentage(), ' ', ' ']
            pbar = progressbar.ProgressBar(widgets=widgets,
                                           maxval=self.cluster_size,
                                           force_update=True)
            self._progress_bar = pbar
        return self._progress_bar

    @property
    def pool(self):
        if not self._pool:
            self._pool = threadpool.get_thread_pool(
                size=self.num_threads, disable_threads=self.disable_threads)
        return self._pool

    @property
    def validator(self):
        return ClusterValidator(self)

    def is_valid(self):
        return self.validator.is_valid()

    def validate(self):
        return self.validator.validate()

    def wait_for_active_spots(self, spots=None):
        """
        Wait for all open spot requests for this cluster to transition to
        'active'.
        """
        spots = spots or self.spot_requests
        open_spots = [spot for spot in spots if spot.state == "open"]
        if open_spots:
            pbar = self.progress_bar.reset()
            log.info('Waiting for open spot requests to become active...')
            pbar.maxval = len(spots)
            pbar.update(0)
            while not pbar.finished:
                active_spots = filter(lambda x: x.state == "active", spots)
                pbar.maxval = len(spots)
                pbar.update(len(active_spots))
                if not pbar.finished:
                    time.sleep(self.refresh_interval)
                    spots = self.get_spot_requests_or_raise()
            pbar.reset()

    def wait_for_active_instances(self, nodes=None):
        """
        Wait indefinitely for cluster nodes to show up.
        """
        nodes = nodes or self.nodes
        if len(nodes) == 0:
            s = self.get_spinner("Waiting for instances to activate...")
            try:
                while len(nodes) == 0:
                    time.sleep(self.refresh_interval)
                    nodes = self.nodes
            finally:
                s.stop()

    def wait_for_running_instances(self, nodes=None):
        """
        Wait until all cluster nodes are in a 'running' state
        """
        log.info("Waiting for all nodes to be in a 'running' state...")
        nodes = nodes or self.get_nodes_or_raise()
        pbar = self.progress_bar.reset()
        pbar.maxval = len(nodes)
        pbar.update(0)
        while not pbar.finished:
            running_nodes = filter(lambda x: x.state == "running", nodes)
            pbar.maxval = len(nodes)
            pbar.update(len(running_nodes))
            if not pbar.finished:
                time.sleep(self.refresh_interval)
                nodes = self.get_nodes_or_raise()
        pbar.reset()

    def wait_for_ssh(self, nodes=None, reboot_interval=10,
                     n_reboot_restart=False):
        """
        Wait until all cluster nodes are in a 'running' state
        """
        log.info("Waiting for SSH to come up on all nodes...")
        nodes = nodes or self.get_nodes_or_raise()
        params = {"interval": self.refresh_interval,
                  "reboot_interval": reboot_interval,
                  "n_reboot_restart": n_reboot_restart}
        self.pool.map(lambda n: n.wait(**params), nodes)

    @print_timing("Waiting for cluster to come up")
    def wait_for_cluster(self, msg="Waiting for cluster to come up...",
                         reboot_interval=10, n_reboot_restart=False):
        """
        Wait for cluster to come up and display progress bar. Waits for all
        spot requests to become 'active', all instances to be in a 'running'
        state, and for all SSH daemons to come up.

        msg - custom message to print out before waiting on the cluster
        """
        interval = self.refresh_interval
        log.info("%s %s" % (msg, "(updating every %ds)" % interval))
        try:
            self.wait_for_active_spots()
            self.wait_for_active_instances()
            self.wait_for_running_instances()
            self.wait_for_ssh(reboot_interval, n_reboot_restart)
        except Exception:
            self.progress_bar.finish()
            raise

    def is_cluster_stopped(self):
        """
        Check whether all nodes are in the 'stopped' state
        """
        nodes = self.nodes
        if not nodes:
            return False
        for node in nodes:
            if node.state != 'stopped':
                return False
        return True

    def is_cluster_terminated(self):
        """
        Check whether all nodes are in a 'terminated' state
        """
        states = filter(lambda x: x != 'terminated', static.INSTANCE_STATES)
        filters = {'group-name': self._security_group,
                   'instance-state-name': states}
        insts = self.ec2.get_all_instances(filters=filters)
        return len(insts) == 0

    def attach_volumes_to_master(self):
        """
        Attach each volume to the master node
        """
        for vol in self.volumes:
            volume = self.volumes.get(vol)
            device = volume.get('device')
            vol_id = volume.get('volume_id')
            vol = self.ec2.get_volume(vol_id)
            if vol.attach_data.instance_id == self.master_node.id:
                log.info("Volume %s already attached to master...skipping" %
                         vol.id)
                continue
            if vol.status != "available":
                log.error('Volume %s not available...'
                          'please check and try again' % vol.id)
                continue
            log.info("Attaching volume %s to master node on %s ..." %
                     (vol.id, device))
            resp = vol.attach(self.master_node.id, device)
            log.debug("resp = %s" % resp)
            self.ec2.wait_for_volume(vol, state='attached')

    def detach_volumes(self):
        """
        Detach all volumes from all nodes
        """
        for node in self.nodes:
            node.detach_external_volumes()

    @print_timing('Restarting cluster')
    def restart_cluster(self):
        """
        Reboot all instances and reconfigure the cluster
        """
        nodes = self.nodes
        if not nodes:
            raise exception.ClusterValidationError("No running nodes found")
        self.run_plugins(method_name="on_restart", reverse=True)
        log.info("Rebooting cluster...")
        for node in nodes:
            node.reboot()
        sleep = 20
        log.info("Sleeping for %d seconds..." % sleep)
        time.sleep(sleep)
        self.setup_cluster()

    def stop_cluster(self, terminate_unstoppable=False, force=False):
        """
        Shutdown this cluster by detaching all volumes and 'stopping' all nodes

        In general, all nodes in the cluster must be 'stoppable' meaning all
        nodes are backed by flat-rate EBS-backed instances. If any
        'unstoppable' nodes are found an exception is raised. A node is
        'unstoppable' if it is backed by either a spot or S3-backed instance.

        If the cluster contains a mix of 'stoppable' and 'unstoppable' nodes
        you can stop all stoppable nodes and terminate any unstoppable nodes by
        setting terminate_unstoppable=True.
        """
        nodes = self.nodes
        if not nodes:
            raise exception.ClusterValidationError("No running nodes found")
        if not self.is_stoppable():
            has_stoppable_nodes = self.has_stoppable_nodes()
            if not terminate_unstoppable and has_stoppable_nodes:
                raise exception.InvalidOperation(
                    "Cluster contains nodes that are not stoppable")
            if not has_stoppable_nodes:
                raise exception.InvalidOperation(
                    "Cluster does not contain any stoppable nodes")
        try:
            self.run_plugins(method_name="on_shutdown", reverse=True)
        except exception.MasterDoesNotExist, e:
            if force:
                log.warn("Cannot run plugins: %s" % e)
            else:
                raise
        self.detach_volumes()
        for node in nodes:
            node.shutdown()

    def terminate_cluster(self, force=False):
        """
        Destroy this cluster by first detaching all volumes, shutting down all
        instances, canceling all spot requests (if any), removing its placement
        group (if any), and removing its security group.
        """
        try:
            self.run_plugins(method_name="on_shutdown", reverse=True)
        except exception.MasterDoesNotExist, e:
            if force:
                log.warn("Cannot run plugins: %s" % e)
            else:
                raise
        self.detach_volumes()
        nodes = self.nodes
        for node in nodes:
            node.terminate()
        for spot in self.spot_requests:
            if spot.state not in ['cancelled', 'closed']:
                log.info("Canceling spot instance request: %s" % spot.id)
                spot.cancel()
        sg = self.ec2.get_group_or_none(self._security_group)
        pg = self.ec2.get_placement_group_or_none(self._security_group)
        s = self.get_spinner("Waiting for cluster to terminate...")
        try:
            while not self.is_cluster_terminated():
                time.sleep(5)
        finally:
            s.stop()
        if pg:
            log.info("Removing %s placement group" % pg.name)
            pg.delete()
        if sg:
            log.info("Removing %s security group" % sg.name)
            sg.delete()

    def start(self, create=True, create_only=False, validate=True,
              validate_only=False, validate_running=False):
        """
        Creates and configures a cluster from this cluster template's settings.

        create - create new nodes when starting the cluster. set to False to
                 use existing nodes
        create_only - only create the cluster node instances, don't configure
                      the cluster
        validate - whether or not to validate the cluster settings used.
                   False will ignore validate_only and validate_running
                   keywords and is effectively the same as running _start
        validate_only - only validate cluster settings, do not create or
                        configure cluster
        validate_running - whether or not to validate the existing instances
                           being used against this cluster's settings
        """
        if validate:
            validator = self.validator
            if not create and validate_running:
                try:
                    validator.validate_running_instances()
                except exception.ClusterValidationError, e:
                    msg = "Existing nodes are not compatible with cluster "
                    msg += "settings:\n"
                    e.msg = msg + e.msg
                    raise
            validator.validate()
            if validate_only:
                return
        else:
            log.warn("SKIPPING VALIDATION - USE AT YOUR OWN RISK")
        return self._start(create=create, create_only=create_only)

    @print_timing("Starting cluster")
    def _start(self, create=True, create_only=False):
        """
        Create and configure a cluster from this cluster template's settings
        (Does not attempt to validate before running)

        create - create new nodes when starting the cluster. set to False to
                 use existing nodes
        create_only - only create the cluster node instances, don't configure
                      the cluster
        """
        log.info("Starting cluster...")
        if create:
            self.create_cluster()
        else:
            assert self.master_node is not None
            for node in self.stopped_nodes:
                log.info("Starting stopped node: %s" % node.alias)
                node.start()
        if create_only:
            return
        self.setup_cluster()

    def setup_cluster(self):
        """
        Waits for all nodes to come up and then runs the default
        StarCluster setup routines followed by any additional plugin setup
        routines
        """
        self.wait_for_cluster()
        self._setup_cluster()

    @print_timing("Configuring cluster")
    def _setup_cluster(self):
        """
        Runs the default StarCluster setup routines followed by any additional
        plugin setup routines. Does not wait for nodes to come up.
        """
        log.info("The master node is %s" % self.master_node.dns_name)
        log.info("Setting up the cluster...")
        if self.volumes:
            self.attach_volumes_to_master()
        default_plugin = clustersetup.DefaultClusterSetup(
            disable_threads=self.disable_threads, num_threads=self.num_threads)
        default_plugin.run(self.nodes, self.master_node, self.cluster_user,
                           self.cluster_shell, self.volumes)
        if not self.disable_queue:
            sge_plugin = sge.SGEPlugin(disable_threads=self.disable_threads,
                                       num_threads=self.num_threads)
            sge_plugin.run(self.nodes, self.master_node, self.cluster_user,
                           self.cluster_shell, self.volumes)
        self.run_plugins()

    def run_plugins(self, plugins=None, method_name="run", node=None,
                    reverse=False):
        """
        Run all plugins specified in this Cluster object's self.plugins list
        Uses plugins list instead of self.plugins if specified.

        plugins must be a tuple: the first element is the plugin's name, the
        second element is the plugin object (a subclass of ClusterSetup)
        """
        plugs = plugins or self.plugins
        if reverse:
            plugs = plugs[:]
            plugs.reverse()
        for plug in plugs:
            self.run_plugin(plug, method_name=method_name, node=node)

    def run_plugin(self, plugin, name='', method_name='run', node=None):
        """
        Run a StarCluster plugin.

        plugin - an instance of the plugin's class
        name - a user-friendly label for the plugin
        method_name - the method to run within the plugin (default: "run")
        node - optional node to pass as first argument to plugin method (used
        for on_add_node/on_remove_node)
        """
        plugin_name = name or getattr(plugin, '__name__',
                                      utils.get_fq_class_name(plugin))
        try:
            func = getattr(plugin, method_name, None)
            if not func:
                log.warn("Plugin %s has no %s method...skipping" %
                         (plugin_name, method_name))
                return
            args = [self.nodes, self.master_node, self.cluster_user,
                    self.cluster_shell, self.volumes]
            if node:
                args.insert(0, node)
            log.info("Running plugin %s" % plugin_name)
            func(*args)
        except NotImplementedError:
            log.debug("method %s not implemented by plugin %s" % (method_name,
                                                                  plugin_name))
        except exception.MasterDoesNotExist:
            raise
        except KeyboardInterrupt:
            raise
        except Exception, e:
            msg = "Error occurred while running plugin '%s':" % plugin_name
            if isinstance(e, exception.ThreadPoolException):
                log.error('\n'.join([msg, e.format_excs()]))
            else:
                log.error(msg, exc_info=True)

    def ssh_to_master(self, user='root', command=None, forward_x11=False,
                      forward_agent=False):
        return self.ssh_to_node('master', user=user, command=command,
                                forward_x11=forward_x11,
                                forward_agent=forward_agent)

    def ssh_to_node(self, alias, user='root', command=None, forward_x11=False,
                    forward_agent=False):
        node = self.get_node_by_alias(alias)
        node = node or self.get_node_by_dns_name(alias)
        node = node or self.get_node_by_id(alias)
        if not node:
            raise exception.InstanceDoesNotExist(alias, label='node')
        return node.shell(user=user, forward_x11=forward_x11,
                          forward_agent=forward_agent, command=command)


class ClusterValidator(validators.Validator):
    """
    Validates that cluster settings define a sane launch configuration.
    Throws exception.ClusterValidationError for all validation failures
    """
    def __init__(self, cluster):
        self.cluster = cluster

    def is_running_valid(self):
        """
        Checks whether the current running instances are compatible
        with this cluster template's settings
        """
        try:
            self.validate_running_instances()
            return True
        except exception.ClusterValidationError, e:
            log.error(e.msg)
            return False

    def validate_required_settings(self):
        has_all_required = True
        for opt in static.CLUSTER_SETTINGS:
            requirements = static.CLUSTER_SETTINGS[opt]
            name = opt
            required = requirements[1]
            if required and self.cluster.get(name.lower()) is None:
                log.warn('Missing required setting %s' % name)
                has_all_required = False
        return has_all_required

    def validate_running_instances(self):
        """
        Validate existing instances against this cluster's settings
        """
        cluster = self.cluster
        cluster.wait_for_active_spots()
        nodes = cluster.nodes
        if not nodes:
            raise exception.ClusterValidationError("No existing nodes found!")
        log.info("Validating existing instances...")
        mazone = cluster.master_node.placement
        # reset zone cache
        cluster._zone = None
        if cluster.zone and cluster.zone.name != mazone:
            raise exception.ClusterValidationError(
                "Running cluster's availability_zone (%s) != %s" %
                (mazone, cluster.zone.name))
        for node in nodes:
            if node.key_name != cluster.keyname:
                raise exception.ClusterValidationError(
                    "%s's key_name (%s) != %s" % (node.alias, node.key_name,
                                                  cluster.keyname))

    def validate(self):
        """
        Checks that all cluster template settings are valid and raises an
        exception.ClusterValidationError exception if not.
        """
        log.info("Validating cluster template settings...")
        try:
            self.validate_required_settings()
            self.validate_spot_bid()
            self.validate_cluster_size()
            self.validate_cluster_user()
            self.validate_shell_setting()
            self.validate_permission_settings()
            self.validate_credentials()
            self.validate_keypair()
            self.validate_zone()
            self.validate_ebs_settings()
            self.validate_ebs_aws_settings()
            self.validate_image_settings()
            self.validate_instance_types()
            self.validate_cluster_compute()
            self.validate_userdata()
            log.info('Cluster template settings are valid')
            return True
        except exception.ClusterValidationError, e:
            e.msg = 'Cluster settings are not valid:\n%s' % e.msg
            raise

    def is_valid(self):
        """
        Returns True if all cluster template settings are valid
        """
        try:
            self.validate()
            return True
        except exception.ClusterValidationError, e:
            log.error(e.msg)
            return False

    def validate_spot_bid(self):
        cluster = self.cluster
        if cluster.spot_bid is not None:
            if type(cluster.spot_bid) not in [int, float]:
                raise exception.ClusterValidationError(
                    'spot_bid must be integer or float')
            if cluster.spot_bid <= 0:
                raise exception.ClusterValidationError(
                    'spot_bid must be an integer or float > 0')
        return True

    def validate_cluster_size(self):
        cluster = self.cluster
        try:
            int(cluster.cluster_size)
            if cluster.cluster_size < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise exception.ClusterValidationError(
                'cluster_size must be an integer >= 1')
        num_itypes = sum([i.get('size') for i in
                          cluster.node_instance_types])
        num_nodes = cluster.cluster_size - 1
        if num_itypes > num_nodes:
            raise exception.ClusterValidationError(
                "total number of nodes specified in node_instance_type (%s) "
                "must be <= cluster_size-1 (%s)" % (num_itypes, num_nodes))
        return True

    def validate_cluster_user(self):
        if self.cluster.cluster_user == "root":
            raise exception.ClusterValidationError(
                'cluster_user cannot be "root"')
        return True

    def validate_shell_setting(self):
        cluster_shell = self.cluster.cluster_shell
        if not static.AVAILABLE_SHELLS.get(cluster_shell):
            raise exception.ClusterValidationError(
                'Invalid user shell specified. Options are %s' %
                ' '.join(static.AVAILABLE_SHELLS.keys()))
        return True

    def validate_image_settings(self):
        cluster = self.cluster
        master_image_id = cluster.master_image_id
        node_image_id = cluster.node_image_id
        conn = cluster.ec2
        image = conn.get_image_or_none(node_image_id)
        if not image or image.id != node_image_id:
            raise exception.ClusterValidationError(
                'node_image_id %s does not exist' % node_image_id)
        if image.state != 'available':
            raise exception.ClusterValidationError(
                'node_image_id %s is not available' % node_image_id)
        if master_image_id:
            master_image = conn.get_image_or_none(master_image_id)
            if not master_image or master_image.id != master_image_id:
                raise exception.ClusterValidationError(
                    'master_image_id %s does not exist' % master_image_id)
            if master_image.state != 'available':
                raise exception.ClusterValidationError(
                    'master_image_id %s is not available' % master_image_id)
        return True

    def validate_zone(self):
        """
        Validates that the cluster's availability zone exists and is available.
        The 'zone' property additionally checks that all EBS volumes are in the
        same zone and that the cluster's availability zone setting, if
        specified, matches the EBS volume(s) zone.
        """
        zone = self.cluster.zone
        if zone and zone.state != 'available':
            raise exception.ClusterValidationError(
                "The '%s' availability zone is not available at this time" %
                zone.name)
        return True

    def __check_platform(self, image_id, instance_type):
        """
        Validates whether an image_id (AMI) is compatible with a given
        instance_type. image_id_setting and instance_type_setting are the
        setting labels in the config file.
        """
        image = self.cluster.ec2.get_image_or_none(image_id)
        if not image:
            raise exception.ClusterValidationError('Image %s does not exist' %
                                                   image_id)
        image_platform = image.architecture
        image_is_hvm = (image.virtualization_type == "hvm")
        if image_is_hvm and instance_type not in static.HVM_TYPES:
            cctypes_list = ', '.join(static.HVM_TYPES)
            raise exception.ClusterValidationError(
                "Image '%s' is a hardware virtual machine (HVM) image and "
                "cannot be used with instance type '%s'.\n\nHVM images "
                "require one of the following HVM instance types:\n%s" %
                (image_id, instance_type, cctypes_list))
        if instance_type in static.CLUSTER_TYPES and not image_is_hvm:
            raise exception.ClusterValidationError(
                "The '%s' instance type can only be used with hardware "
                "virtual machine (HVM) images. Image '%s' is not an HVM "
                "image." % (instance_type, image_id))
        instance_platforms = static.INSTANCE_TYPES[instance_type]
        if image_platform not in instance_platforms:
            error_msg = "Instance type %(instance_type)s is for an " \
                        "%(instance_platform)s platform while " \
                        "%(image_id)s is an %(image_platform)s platform"
            error_dict = {'instance_type': instance_type,
                          'instance_platform': ', '.join(instance_platforms),
                          'image_id': image_id,
                          'image_platform': image_platform}
            raise exception.ClusterValidationError(error_msg % error_dict)
        image_is_ebs = (image.root_device_type == 'ebs')
        ebs_only_types = static.MICRO_INSTANCE_TYPES + static.SEC_GEN_TYPES
        if instance_type in ebs_only_types and not image_is_ebs:
            error_msg = ("Instance type %s can only be used with an "
                         "EBS-backed AMI and '%s' is not EBS-backed " %
                         (instance_type, image.id))
            raise exception.ClusterValidationError(error_msg)
        return True

    def validate_instance_types(self):
        cluster = self.cluster
        master_image_id = cluster.master_image_id
        node_image_id = cluster.node_image_id
        master_instance_type = cluster.master_instance_type
        node_instance_type = cluster.node_instance_type
        instance_types = static.INSTANCE_TYPES
        instance_type_list = ', '.join(instance_types.keys())
        if not node_instance_type in instance_types:
            raise exception.ClusterValidationError(
                "You specified an invalid node_instance_type %s\n"
                "Possible options are:\n%s" %
                (node_instance_type, instance_type_list))
        elif master_instance_type:
            if not master_instance_type in instance_types:
                raise exception.ClusterValidationError(
                    "You specified an invalid master_instance_type %s\n"
                    "Possible options are:\n%s" %
                    (master_instance_type, instance_type_list))
        try:
            self.__check_platform(node_image_id, node_instance_type)
        except exception.ClusterValidationError, e:
            raise exception.ClusterValidationError(
                'Incompatible node_image_id and node_instance_type:\n' + e.msg)
        if master_image_id and not master_instance_type:
            try:
                self.__check_platform(master_image_id, node_instance_type)
            except exception.ClusterValidationError, e:
                raise exception.ClusterValidationError(
                    'Incompatible master_image_id and node_instance_type\n' +
                    e.msg)
        elif master_image_id and master_instance_type:
            try:
                self.__check_platform(master_image_id, master_instance_type)
            except exception.ClusterValidationError, e:
                raise exception.ClusterValidationError(
                    'Incompatible master_image_id and master_instance_type\n' +
                    e.msg)
        elif master_instance_type and not master_image_id:
            try:
                self.__check_platform(node_image_id, master_instance_type)
            except exception.ClusterValidationError, e:
                raise exception.ClusterValidationError(
                    'Incompatible node_image_id and master_instance_type\n' +
                    e.msg)
        for itype in cluster.node_instance_types:
            type = itype.get('type')
            img = itype.get('image') or node_image_id
            if not type in instance_types:
                raise exception.ClusterValidationError(
                    "You specified an invalid instance type %s\n"
                    "Possible options are:\n%s" % (type, instance_type_list))
            try:
                self.__check_platform(img, type)
            except exception.ClusterValidationError, e:
                raise exception.ClusterValidationError(
                    "Invalid settings for node_instance_type %s: %s" %
                    (type, e.msg))
        return True

    def validate_cluster_compute(self):
        cluster = self.cluster
        lmap = cluster._get_launch_map()
        for (type, image) in lmap:
            if type in static.CLUSTER_TYPES:
                img = cluster.ec2.get_image(image)
                if img.virtualization_type != 'hvm':
                    raise exception.ClusterValidationError(
                        'Cluster Compute/GPU instance type %s '
                        'can only be used with HVM images.\n'
                        'Image %s is NOT an HVM image.' % (type, image))

    def validate_permission_settings(self):
        permissions = self.cluster.permissions
        for perm in permissions:
            permission = permissions.get(perm)
            protocol = permission.get('ip_protocol')
            if protocol not in static.PROTOCOLS:
                raise exception.InvalidProtocol(protocol)
            from_port = permission.get('from_port')
            to_port = permission.get('to_port')
            try:
                from_port = int(from_port)
                to_port = int(to_port)
            except ValueError:
                raise exception.InvalidPortRange(
                    from_port, to_port, reason="integer range required")
            if from_port < 0 or to_port < 0:
                raise exception.InvalidPortRange(
                    from_port, to_port,
                    reason="from/to must be positive integers")
            if from_port > to_port:
                raise exception.InvalidPortRange(
                    from_port, to_port,
                    reason="'from_port' must be <= 'to_port'")
            cidr_ip = permission.get('cidr_ip')
            if not iptools.validate_cidr(cidr_ip):
                raise exception.InvalidCIDRSpecified(cidr_ip)

    def validate_ebs_settings(self):
        """
        Check EBS vols for missing/duplicate DEVICE/PARTITION/MOUNT_PATHs and
        validate these settings.
        """
        volmap = {}
        devmap = {}
        mount_paths = []
        cluster = self.cluster
        for vol in cluster.volumes:
            vol_name = vol
            vol = cluster.volumes.get(vol)
            vol_id = vol.get('volume_id')
            device = vol.get('device')
            partition = vol.get('partition')
            mount_path = vol.get("mount_path")
            vmap = volmap.get(vol_id, {})
            devices = vmap.get('device', [])
            partitions = vmap.get('partition', [])
            if devices and device not in devices:
                raise exception.ClusterValidationError(
                    "Can't attach volume %s to more than one device" % vol_id)
            elif partitions and partition in partitions:
                raise exception.ClusterValidationError(
                    "Multiple configurations for %s\n"
                    "Either pick one or specify a separate partition for "
                    "each configuration" % vol_id)
            vmap['partition'] = partitions + [partition]
            vmap['device'] = devices + [device]
            volmap[vol_id] = vmap
            dmap = devmap.get(device, {})
            vol_ids = dmap.get('volume_id', [])
            if vol_ids and vol_id not in vol_ids:
                raise exception.ClusterValidationError(
                    "Can't attach more than one volume on device %s" % device)
            dmap['volume_id'] = vol_ids + [vol_id]
            devmap[device] = dmap
            mount_paths.append(mount_path)
            if not device:
                raise exception.ClusterValidationError(
                    'Missing DEVICE setting for volume %s' % vol_name)
            if not utils.is_valid_device(device):
                raise exception.ClusterValidationError(
                    "Invalid DEVICE value for volume %s" % vol_name)
            if partition:
                if not utils.is_valid_partition(partition):
                    raise exception.ClusterValidationError(
                        "Invalid PARTITION value for volume %s" % vol_name)
                if not partition.startswith(device):
                    raise exception.ClusterValidationError(
                        "Volume PARTITION must start with %s" % device)
            if not mount_path:
                raise exception.ClusterValidationError(
                    'Missing MOUNT_PATH setting for volume %s' % vol_name)
            if not mount_path.startswith('/'):
                raise exception.ClusterValidationError(
                    "MOUNT_PATH for volume %s should start with /" % vol_name)
        for path in mount_paths:
            if mount_paths.count(path) > 1:
                raise exception.ClusterValidationError(
                    "Can't mount more than one volume on %s" % path)
        return True

    def validate_ebs_aws_settings(self):
        """
        Verify that all EBS volumes exist and are available.
        """
        cluster = self.cluster
        for vol in cluster.volumes:
            v = cluster.volumes.get(vol)
            vol_id = v.get('volume_id')
            vol = cluster.ec2.get_volume(vol_id)
            if vol.status != 'available':
                try:
                    if vol.attach_data.instance_id == cluster.master_node.id:
                        continue
                except exception.MasterDoesNotExist:
                    pass
                raise exception.ClusterValidationError(
                    "Volume '%s' is not available (status: %s)" %
                    (vol_id, vol.status))

    def validate_credentials(self):
        if not self.cluster.ec2.is_valid_conn():
            raise exception.ClusterValidationError(
                'Invalid AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY combination.')
        return True

    def validate_keypair(self):
        cluster = self.cluster
        key_location = cluster.key_location
        if not key_location:
            raise exception.ClusterValidationError(
                "no key_location specified for key '%s'" %
                cluster.keyname)
        if not os.path.exists(key_location):
            raise exception.ClusterValidationError(
                "key_location '%s' does not exist" % key_location)
        elif not os.path.isfile(key_location):
            raise exception.ClusterValidationError(
                "key_location '%s' is not a file" % key_location)
        keyname = cluster.keyname
        keypair = cluster.ec2.get_keypair_or_none(keyname)
        if not keypair:
            raise exception.ClusterValidationError(
                "Keypair '%s' does not exist in region '%s'" %
                (keyname, cluster.ec2.region.name))
        fingerprint = keypair.fingerprint
        try:
            open(key_location, 'r').close()
        except IOError, e:
            raise exception.ClusterValidationError(
                "Error loading key_location '%s':\n%s\n"
                "Please check that the file is readable" % (key_location, e))
        if len(fingerprint) == 59:
            keyfingerprint = sshutils.get_private_rsa_fingerprint(key_location)
            if keyfingerprint != fingerprint:
                raise exception.ClusterValidationError(
                    "Incorrect fingerprint for key_location '%s'\n\n"
                    "local fingerprint: %s\n\nkeypair fingerprint: %s"
                    % (key_location, keyfingerprint, fingerprint))
        else:
            # Skip fingerprint validation for keys created using EC2 import
            # keys until I can figure out the mystery behind the import keys
            # fingerprint. I'm able to match ssh-keygen's public key
            # fingerprint, however, Amazon doesn't for some reason...
            log.warn("Unable to validate imported keypair fingerprint...")
        return True

    def validate_userdata(self):
        for script in self.cluster.userdata_scripts:
            if not os.path.exists(script):
                raise exception.ClusterValidationError(
                    "Userdata script does not exist: %s" % script)
            if not os.path.isfile(script):
                raise exception.ClusterValidationError(
                    "Userdata script is not a file: %s" % script)
        cluster_size = self.cluster.cluster_size
        node_aliases = ["node%0.3d" % i for i in range(1, cluster_size)]
        ud = self.cluster._get_cluster_userdata(["master"] + node_aliases)
        ud_size_kb = utils.size_in_kb(ud)
        if ud_size_kb > 16:
            raise exception.ClusterValidationError(
                "User data is too big! (%.2fKB)\n"
                "User data scripts combined and compressed must be <= 16KB\n"
                "NOTE: StarCluster uses anywhere from 0.5-2KB "
                "to store internal metadata" % ud_size_kb)
