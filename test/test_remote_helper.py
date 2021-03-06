#!/usr/bin/env python
# Programmer: Chris Bunch, Brian Drawert


# General-purpose Python library imports
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest


# Third party libraries
import boto
from flexmock import flexmock
import SOAPpy


# AppScale import, the library that we're testing here
lib = os.path.dirname(__file__) + os.sep + ".." + os.sep + "lib"
sys.path.append(lib)
from agents.euca_agent import EucalyptusAgent
from appcontroller_client import AppControllerClient
from appscale_logger import AppScaleLogger
from appscale_tools import AppScaleTools
from custom_exceptions import AppScaleException
from custom_exceptions import BadConfigurationException
from custom_exceptions import ShellException
from local_state import APPSCALE_VERSION
from local_state import LocalState
from node_layout import NodeLayout
from remote_helper import RemoteHelper


class TestRemoteHelper(unittest.TestCase):


  def setUp(self):
    # mock out all logging, since it clutters our output
    flexmock(AppScaleLogger)
    AppScaleLogger.should_receive('log').and_return()

    # mock out all sleeps, as they aren't necessary for unit testing
    flexmock(time)
    time.should_receive('sleep').and_return()

    # set up some fake options so that we don't have to generate them via
    # ParseArgs
    self.options = flexmock(infrastructure='ec2', group='boogroup',
      machine='ami-ABCDEFG', instance_type='m1.large', keyname='bookey',
      table='cassandra', verbose=False, test=False, use_spot_instances=False)
    self.my_id = "12345"
    self.node_layout = NodeLayout(self.options)

    # set up phony AWS credentials for each test
    # ones that test not having them present can
    # remove them
    for credential in EucalyptusAgent.REQUIRED_EC2_CREDENTIALS:
      os.environ[credential] = "baz"
    os.environ['EC2_URL'] = "http://boo"

    # mock out calls to EC2
    # begin by assuming that our ssh keypair doesn't exist, and thus that we
    # need to create it
    key_contents = "key contents here"
    fake_key = flexmock(name="fake_key", material=key_contents)
    fake_key.should_receive('save').with_args(os.environ['HOME']+'/.appscale').and_return(None)

    fake_ec2 = flexmock(name="fake_ec2")
    fake_ec2.should_receive('get_key_pair').with_args('bookey') \
      .and_return(None)
    fake_ec2.should_receive('create_key_pair').with_args('bookey') \
      .and_return(fake_key)

    # mock out writing the secret key
    builtins = flexmock(sys.modules['__builtin__'])
    builtins.should_call('open')  # set the fall-through

    secret_key_location = LocalState.LOCAL_APPSCALE_PATH + "bookey.secret"
    fake_secret = flexmock(name="fake_secret")
    fake_secret.should_receive('write').and_return()
    builtins.should_receive('open').with_args(secret_key_location, 'w') \
      .and_return(fake_secret)

    # also, mock out the keypair writing and chmod'ing
    ssh_key_location = LocalState.LOCAL_APPSCALE_PATH + "bookey.key"
    fake_file = flexmock(name="fake_file")
    fake_file.should_receive('write').with_args(key_contents).and_return()

    builtins.should_receive('open').with_args(ssh_key_location, 'w') \
      .and_return(fake_file)

    flexmock(os)
    os.should_receive('chmod').with_args(ssh_key_location, 0600).and_return()

    # next, assume there are no security groups up yet
    fake_ec2.should_receive('get_all_security_groups').and_return([])

    # and then assume we can create and open our security group fine
    fake_ec2.should_receive('create_security_group').with_args('boogroup',
      'AppScale security group').and_return()
    fake_ec2.should_receive('authorize_security_group').and_return()

    # next, add in mocks for run_instances
    # the first time around, let's say that no machines are running
    # the second time around, let's say that our machine is pending
    # and that it's up the third time around
    fake_pending_instance = flexmock(state='pending')
    fake_pending_reservation = flexmock(instances=fake_pending_instance)

    fake_running_instance = flexmock(state='running', key_name='bookey',
      id='i-12345678', public_dns_name='public1', private_dns_name='private1')
    fake_running_reservation = flexmock(instances=fake_running_instance)

    fake_ec2.should_receive('get_all_instances').and_return([]) \
      .and_return([fake_pending_reservation]) \
      .and_return([fake_running_reservation])

    # next, assume that our run_instances command succeeds
    fake_ec2.should_receive('run_instances').and_return()

    # finally, inject our mocked EC2
    flexmock(boto)
    boto.should_receive('connect_ec2').and_return(fake_ec2)

    # assume that ssh comes up on the third attempt
    fake_socket = flexmock(name='fake_socket')
    fake_socket.should_receive('connect').with_args(('public1',
      RemoteHelper.SSH_PORT)).and_raise(Exception).and_raise(Exception) \
      .and_return(None)
    flexmock(socket)
    socket.should_receive('socket').and_return(fake_socket)

    # throw some default mocks together for when invoking via shell succeeds
    # and when it fails
    self.fake_temp_file = flexmock(name='fake_temp_file')
    self.fake_temp_file.should_receive('seek').with_args(0).and_return()
    self.fake_temp_file.should_receive('read').and_return('boo out')
    self.fake_temp_file.should_receive('close').and_return()


    flexmock(tempfile)
    tempfile.should_receive('NamedTemporaryFile')\
      .and_return(self.fake_temp_file)

    self.success = flexmock(name='success', returncode=0)
    self.success.should_receive('wait').and_return(0)

    self.failed = flexmock(name='success', returncode=1)
    self.failed.should_receive('wait').and_return(1)

    # assume that root login isn't already enabled
    local_state = flexmock(LocalState)
    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh .*root'), False, 1, stdin='ls') \
      .and_return('Please login as the ubuntu user rather than root user.')

    # and assume that we can ssh in as ubuntu to enable root login
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh .*ubuntu'),False,5)\
      .and_return()

    # also assume that we can scp over our ssh keys
    local_state.should_receive('shell')\
      .with_args(re.compile('scp .*/root/.ssh/id_'),False,5)\
      .and_return()

    local_state.should_receive('shell')\
      .with_args(re.compile('scp .*/root/.appscale/bookey.key'),False,5)\
      .and_return()


  def test_start_head_node_in_cloud_but_ami_not_appscale(self):
    # mock out our attempts to find /etc/appscale and presume it doesn't exist
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh'),False,5,stdin=re.compile('^sudo cp'))\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh'),False,5,\
        stdin=re.compile('ls /etc/appscale'))\
      .and_raise(ShellException).ordered()

    # check that the cleanup routine is called on error
    flexmock(AppScaleTools).should_receive('terminate_instances')\
      .and_return().ordered()

    self.assertRaises(AppScaleException, RemoteHelper.start_head_node,
      self.options, self.my_id, self.node_layout)


  def test_start_head_node_in_cloud_but_ami_wrong_version(self):
    # mock out our attempts to find /etc/appscale and presume it does exist
    local_state = flexmock(LocalState)
    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5, stdin=re.compile('^sudo cp')) \
      .and_return().ordered()

    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5,
        stdin=re.compile('ls /etc/appscale')) \
      .and_return().ordered()

    # mock out our attempts to find /etc/appscale/version and presume it doesn't
    # exist
    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5,
        stdin=re.compile('ls /etc/appscale/{0}'.format(APPSCALE_VERSION)))\
      .and_raise(ShellException).ordered()

    # check that the cleanup routine is called on error
    flexmock(AppScaleTools).should_receive('terminate_instances')\
      .and_return().ordered()

    self.assertRaises(AppScaleException, RemoteHelper.start_head_node,
      self.options, self.my_id, self.node_layout)


  def test_start_head_node_in_cloud_but_using_unsupported_database(self):
    local_state = flexmock(LocalState)

    # mock out our attempts to find /etc/appscale and presume it does exist
    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5, stdin=re.compile('^sudo cp')) \
      .and_return().ordered()

    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5,
        stdin=re.compile('ls /etc/appscale')) \
      .and_return().ordered()

    # mock out our attempts to find /etc/appscale/version and presume it does
    # exist
    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5,
        stdin=re.compile('ls /etc/appscale/{0}'.format(APPSCALE_VERSION))) \
      .and_return().ordered()

    # finally, put in a mock indicating that the database the user wants
    # isn't supported
    local_state.should_receive('shell') \
      .with_args(re.compile('^ssh'), False, 5,
        stdin=re.compile('ls /etc/appscale/{0}/{1}'
          .format(APPSCALE_VERSION, 'cassandra'))) \
      .and_raise(ShellException).ordered()

    # check that the cleanup routine is called on error
    flexmock(AppScaleTools).should_receive('terminate_instances')\
      .and_return().ordered()

    self.assertRaises(AppScaleException, RemoteHelper.start_head_node,
      self.options, self.my_id, self.node_layout)


  def test_rsync_files_from_dir_that_doesnt_exist(self):
    # if the user specifies that we should copy from a directory that doesn't
    # exist, we should throw up and die
    flexmock(os.path)
    os.path.should_receive('exists').with_args('/tmp/booscale-local/lib')\
      .and_return(False)
    self.assertRaises(BadConfigurationException, RemoteHelper.rsync_files,
      'public1', 'booscale', '/tmp/booscale-local', False)


  def test_rsync_files_from_dir_that_does_exist(self):
    # if the user specifies that we should copy from a directory that does
    # exist, and has all the right directories in it, we should succeed
    flexmock(os.path)
    os.path.should_receive('exists').with_args(re.compile(
      '/tmp/booscale-local/')).and_return(True)

    # assume the rsyncs succeed
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^rsync'),False)\
      .and_return().ordered()

    RemoteHelper.rsync_files('public1', 'booscale', '/tmp/booscale-local',
      False)


  def test_copy_deployment_credentials_in_cloud(self):
    # mock out the scp'ing to public1 and assume they succeed
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*secret.key'),True,5)\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*ssh.key'),True,5)\
      .and_return().ordered()

    # mock out generating the private key
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^openssl'),True, stdin=None)\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*mycert.pem'),True,5)\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*mykey.pem'),True,5)\
      .and_return().ordered()

    # next, mock out copying the private key and certificate
    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh'),True,5,stdin=re.compile('^mkdir -p'))\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*cloud1/mycert.pem'),True,5)\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*cloud1/mykey.pem'),True,5)\
      .and_return().ordered()

    options = flexmock(name='options', keyname='bookey', infrastructure='ec2',
      verbose=True)
    RemoteHelper.copy_deployment_credentials('public1', options)


  def test_start_remote_appcontroller(self):
    # mock out removing the old json file
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh'),False,5,stdin=re.compile('rm -rf'))\
      .and_return().ordered()

    # assume we started god on public1 fine
    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh'),False,5,stdin=re.compile('god &'))\
      .and_return().ordered()

    # also assume that we scp'ed over the god config file fine
    local_state.should_receive('shell')\
      .with_args(re.compile('scp .*appcontroller\.god.*'),False,5)\
      .and_return().ordered()

    # and assume we started the AppController on public1 fine
    local_state.should_receive('shell')\
      .with_args(re.compile('^ssh'),False,5,\
        stdin=re.compile('^god load .*appcontroller\.god'))\
      .and_return().ordered()

    # finally, assume the appcontroller comes up after a few tries
    # assume that ssh comes up on the third attempt
    fake_socket = flexmock(name='fake_socket')
    fake_socket.should_receive('connect').with_args(('public1',
      AppControllerClient.PORT)).and_raise(Exception) \
      .and_raise(Exception).and_return(None)
    socket.should_receive('socket').and_return(fake_socket)

    

    RemoteHelper.start_remote_appcontroller('public1', 'bookey', False)


  def test_copy_local_metadata(self):
    # mock out the copying of the two files
    local_state = flexmock(LocalState)
    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*/etc/appscale/locations-bookey.yaml'),\
        False,5)\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*/etc/appscale/locations-bookey.json'),\
        False,5)\
      .and_return().ordered()

    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*/root/.appscale/locations-bookey.json'),\
        False,5)\
      .and_return().ordered()
	
    # and mock out copying the secret file
    local_state.should_receive('shell')\
      .with_args(re.compile('^scp .*bookey.secret'),False,5)\
      .and_return().ordered()

    RemoteHelper.copy_local_metadata('public1', 'bookey', False)


  def test_create_user_accounts(self):
    # mock out reading the secret key
    builtins = flexmock(sys.modules['__builtin__'])
    builtins.should_call('open')  # set the fall-through

    secret_key_location = LocalState.LOCAL_APPSCALE_PATH + "bookey.secret"
    fake_secret = flexmock(name="fake_secret")
    fake_secret.should_receive('read').and_return('the secret')
    builtins.should_receive('open').with_args(secret_key_location, 'r') \
      .and_return(fake_secret)

    # mock out reading the locations.json file, and slip in our own json
    flexmock(os.path)
    os.path.should_call('exists')  # set the fall-through
    os.path.should_receive('exists').with_args(
      LocalState.get_locations_json_location('bookey')).and_return(True)

    fake_nodes_json = flexmock(name="fake_nodes_json")
    fake_nodes_json.should_receive('read').and_return(json.dumps([{
      "public_ip" : "public1",
      "private_ip" : "private1",
      "jobs" : ["shadow", "login"]
    }]))
    builtins.should_receive('open').with_args(
      LocalState.get_locations_json_location('bookey'), 'r') \
      .and_return(fake_nodes_json)

    # mock out SOAP interactions with the UserAppServer
    fake_soap = flexmock(name='fake_soap')
    fake_soap.should_receive('commit_new_user').with_args('boo@foo.goo', str,
      'xmpp_user', 'the secret').and_return('true')
    fake_soap.should_receive('commit_new_user').with_args('boo@public1', str,
      'xmpp_user', 'the secret').and_return('true')
    flexmock(SOAPpy)
    SOAPpy.should_receive('SOAPProxy').with_args('https://public1:4343') \
      .and_return(fake_soap)

    RemoteHelper.create_user_accounts('boo@foo.goo', 'password', 'public1',
      'bookey')


  def test_wait_for_machines_to_finish_loading(self):
    # mock out reading the secret key
    builtins = flexmock(sys.modules['__builtin__'])
    builtins.should_call('open')  # set the fall-through

    secret_key_location = LocalState.LOCAL_APPSCALE_PATH + "bookey.secret"
    fake_secret = flexmock(name="fake_secret")
    fake_secret.should_receive('read').and_return('the secret')
    builtins.should_receive('open').with_args(secret_key_location, 'r') \
      .and_return(fake_secret)

    # mock out getting all the ips in the deployment from the head node
    fake_soap = flexmock(name='fake_soap')
    fake_soap.should_receive('get_all_public_ips').with_args('the secret') \
      .and_return(json.dumps(['public1', 'public2']))
    role_info = [
      {
        'public_ip' : 'public1',
        'private_ip' : 'private1',
        'jobs' : ['shadow', 'db_master']
      },
      {
        'public_ip' : 'public2',
        'private_ip' : 'private2',
        'jobs' : ['appengine']
      }
    ]
    fake_soap.should_receive('get_role_info').with_args('the secret') \
      .and_return(json.dumps(role_info))

    # also, let's say that our machines aren't running the first time we ask,
    # but that they are the second time
    fake_soap.should_receive('is_done_initializing').with_args('the secret') \
      .and_return(False).and_return(True)

    flexmock(SOAPpy)
    SOAPpy.should_receive('SOAPProxy').with_args('https://public1:17443') \
      .and_return(fake_soap)
    SOAPpy.should_receive('SOAPProxy').with_args('https://public2:17443') \
      .and_return(fake_soap)

    RemoteHelper.wait_for_machines_to_finish_loading('public1', 'bookey')
