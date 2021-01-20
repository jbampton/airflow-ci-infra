#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import datetime
import json
import logging
import os
import random
import shutil
import signal
import socket
from typing import Callable, List

import boto3
import click
from python_dynamodb_lock.python_dynamodb_lock import DynamoDBLockClient, DynamoDBLockError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


@click.command()
@click.option('--repo', default='apache/airflow')
@click.option(
    '--output-folder',
    help="Folder to write credentials to. Default of ~runner/actions-runner",
    default='~runner/actions-runner',
)
def main(repo, output_folder):
    log.info("Starting...")
    output_folder = os.path.expanduser(output_folder)

    short_time = datetime.timedelta(microseconds=1)

    dynamodb = boto3.resource('dynamodb')
    client = DynamoDBLockClient(
        dynamodb,
        table_name='GitHubRunnerLocks',
        expiry_period=datetime.timedelta(0, 300),
        heartbeat_period=datetime.timedelta(seconds=10),
    )

    # Just keep trying until we get some credentials.
    while True:
        # Have each runner try to get a credential in a random order.
        possibles = get_possible_credentials(repo)
        random.shuffle(possibles)

        log.info("Trying to get a set of credentials in this order: %r", possibles)

        notify = get_sd_notify_func()

        for index in possibles:
            try:
                lock = client.acquire_lock(
                    f'{repo}/{index}',
                    retry_period=short_time,
                    retry_timeout=short_time,
                    raise_context_exception=True,
                )
            except DynamoDBLockError as e:
                log.info("Could not lock %s (%s)", index, e)
                continue

            with lock:
                log.info("Obtained lock on %s", index)
                write_credentials_to_files(repo, index, output_folder)
                merge_in_settings(repo, output_folder)
                notify(f"STATUS=Obtained lock on {index}")
                complete_asg_lifecycle_hook()

                def sig_handler(signal, frame):
                    # no-op
                    ...

                signal.signal(signal.SIGINT, sig_handler)

                notify("READY=1")
                log.info("Waiting singal")
                while True:
                    # sleep until we are told to shut down
                    signal.pause()
                    log.info("Got signal")
                    break

            client.close()

            exit()


def get_sd_notify_func() -> Callable[[str], None]:
    # http://www.freedesktop.org/software/systemd/man/sd_notify.html
    addr = os.getenv('NOTIFY_SOCKET')
    if not addr:
        return lambda status: None

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    if addr[0] == '@':
        addr = '\0' + addr[1:]
    sock.connect(addr)

    def notify(status: str):
        sock.sendall(status.encode('utf-8'))

    return notify


def write_credentials_to_files(repo: str, index: str, out_folder: str = '~runner/actions-runner'):
    param_path = os.path.join('/runners/', repo, index)

    resp = boto3.client("ssm").get_parameters_by_path(Path=param_path, Recursive=False, WithDecryption=True)

    param_to_file = {
        'config': '.runner',
        'credentials': '.credentials',
        'rsaparams': '.credentials_rsaparams',
    }

    for param in resp['Parameters']:
        # "/runners/apache/airflow/config" -> "config"
        name = os.path.basename(param['Name'])
        filename = param_to_file.get(name, None)
        if filename is None:
            log.info("Unknown Parameter from SSM: %r", param['Name'])
            continue
        log.info("Writing %r to %r", param['Name'], filename)
        with open(os.path.join(out_folder, filename), "w") as fh:
            fh.write(param['Value'])
            shutil.chown(fh.name, 'runner')
            os.chmod(fh.name, 0o600)
        del param_to_file[name]
    if param_to_file:
        raise RuntimeError(f"Missing expected params: {list(param_to_file.keys())}")


def merge_in_settings(repo: str, out_folder: str) -> None:
    client = boto3.client('ssm')

    param_path = os.path.join('/runners/', repo, 'configOverlay')
    log.info("Loading config overlay from %s", param_path)

    try:

        resp = client.get_parameter(Name=param_path, WithDecryption=True)
    except client.exceptions.ParameterNotFound:
        log.debug("Failed to load config overlay", exc_info=True)
        return

    try:
        overlay = json.loads(resp['Parameter']['Value'])
    except ValueError:
        log.debug("Failed to parse config overlay", exc_info=True)
        return

    with open(os.path.join(out_folder, ".runner"), "r+") as fh:
        settings = json.load(fh)

        for key, val in overlay.items():
            settings[key] = val

        fh.seek(0, os.SEEK_SET)
        os.ftruncate(fh.fileno(), 0)
        json.dump(settings, fh, indent=2)


def get_possible_credentials(repo: str) -> List[str]:
    client = boto3.client("ssm")
    paginator = client.get_paginator("describe_parameters")

    path = os.path.join('/runners/', repo, '')
    baked_path = os.path.join(path, 'runnersList')

    # Pre-compute the list, to avoid making lots of requests and getting throttled by SSM API in case of
    # thundering herd
    try:
        log.info("Using pre-computed credentials indexes from %s", baked_path)
        resp = client.get_parameter(Name=baked_path)
        return resp['Parameter']['Value'].split(',')
    except client.exceptions.ParameterNotFound:
        pass

    log.info("Looking at %s for possible credentials", path)

    pages = paginator.paginate(
        ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": [path]}],
        PaginationConfig={
            "PageSize": 50,
        },
    )

    seen = set()

    for i, page in enumerate(pages):
        log.info("Page %d", i)
        for param in page['Parameters']:
            name = param['Name']
            log.info("%s", name)

            # '/runners/x/1/config' -> '1/config',
            # '/runners/x/y/1/config' -> 'y/1/config',
            local_name = name[len(path) :]

            try:
                # '1/config' -> '1'
                index, _ = local_name.split('/')
            except ValueError:
                # Ignore any 'x/y' when we asked for 'x'. There should only be an index and a filename
                log.debug("Ignoring nested path %s", name)
                continue

            try:
                # Check it's a number, but keep variable as string
                int(index)
            except ValueError:
                log.debug("Ignoring non-numeric index %s", name)
                continue

            index = os.path.basename(os.path.dirname(name))
            seen.add(index)

    if not seen:
        raise RuntimeError(f'No credentials found in SSM ParameterStore for {repo!r}')

    try:
        resp = client.put_parameter(
            Name=baked_path, Type='StringList', Value=','.join(list(seen)), Overwrite=False
        )
        log.info("Stored pre-computed credentials indexes at %s", baked_path)
    except client.exceptions.ParameterAlreadyExists:
        # Race, we lost, never mind!
        pass

    return list(seen)


def complete_asg_lifecycle_hook():
    # Notify the ASG LifeCycle hook that we are now In Service and ready to process request

    # Fetch current instance ID from where cloutinit writes it to
    with open('/var/lib/cloud/data/instance-id') as fh:
        instance_id = fh.readline().strip()

    # Get the ASG name we are attached to by looking at our own tags
    ec2 = boto3.client('ec2')
    tags = ec2.describe_tags(
        Filters=[
            {'Name': 'key', 'Values': ['aws:autoscaling:groupName']},
            {'Name': 'resource-id', 'Values': [instance_id]},
        ]
    )

    asg_name = tags['Tags'][0]['Value']

    asg_client = boto3.client('autoscaling')
    asg_client.complete_lifecycle_action(
        AutoScalingGroupName=asg_name,
        InstanceId=instance_id,
        LifecycleHookName='WaitForInstanceReportReady',
        LifecycleActionResult='CONTINUE',
    )


if __name__ == "__main__":
    main()
