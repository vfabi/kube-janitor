import datetime
import logging
import time
from collections import Counter
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional

import pykube
from pykube import Event
from pykube import Namespace
from pykube.objects import APIObject

from .helper import format_duration
from .helper import parse_expiry
from .helper import parse_ttl
from .resource_context import get_resource_context
from .resources import get_namespaced_resource_types

logger = logging.getLogger(__name__)

TTL_ANNOTATION = "janitor/ttl"
EXPIRY_ANNOTATION = "janitor/expires"
NOTIFIED_ANNOTATION = "janitor/notified"


def matches_resource_filter(
    resource,
    include_resources: frozenset,
    exclude_resources: frozenset,
    include_namespaces: frozenset,
    exclude_namespaces: frozenset,
):

    resource_type_plural = resource.endpoint
    if resource.kind == "Namespace":
        namespace = resource.name
    else:
        namespace = resource.namespace

    if namespace is None:
        # skip all non-namespaced resources
        return False

    resource_included = (
        "all" in include_resources or resource_type_plural in include_resources
    )
    namespace_included = "all" in include_namespaces or namespace in include_namespaces
    resource_excluded = (
        "all" in exclude_resources and resource_type_plural not in include_resources
    ) or resource_type_plural in exclude_resources
    namespace_excluded = (
        "all" in exclude_namespaces and namespace not in include_namespaces
    ) or namespace in exclude_namespaces
    return (
        resource_included
        and not resource_excluded
        and namespace_included
        and not namespace_excluded
    )


def parse_time(timestamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")


def get_deployment_time(
    resource, deployment_time_annotation: Optional[str]
) -> datetime.datetime:
    creation_timestamp = parse_time(resource.metadata["creationTimestamp"])

    if deployment_time_annotation:
        annotations = resource.metadata.get("annotations", {})
        deployment_time = annotations.get(deployment_time_annotation)
        if deployment_time:
            try:
                return max(creation_timestamp, parse_time(deployment_time))
            except ValueError as e:
                logger.warning(
                    f"Invalid {deployment_time_annotation} in {resource.namespace}/{resource.name}: {e}"
                )

    return creation_timestamp


def get_delete_notification_time(
    expiry_timestamp, delete_notification
) -> datetime.datetime:
    return expiry_timestamp - datetime.timedelta(seconds=delete_notification)


def add_notification_flag(resource, dry_run: bool):
    if dry_run:
        logger.info(
            f"**DRY-RUN**: {resource.kind} {resource.namespace}/{resource.name} would be annotated as janitor/notified: yes"
        )
    else:
        resource.annotations[NOTIFIED_ANNOTATION] = "yes"
        resource.update()


def was_notified(resource):
    return NOTIFIED_ANNOTATION in resource.annotations.keys()


def utcnow():
    return datetime.datetime.utcnow()


def send_delete_notification(
    resource, reason: str, expire: datetime.datetime, dry_run: bool
):
    formatted_expire_datetime = expire.strftime("%Y-%m-%dT%H:%M:%SZ")
    message = f"{resource.kind} {resource.name} will be deleted at {formatted_expire_datetime} ({reason})"
    logger.info(message)
    create_event(resource, message, "DeleteNotification", dry_run=dry_run)
    add_notification_flag(resource, dry_run=dry_run)


def create_event(resource, message: str, reason: str, dry_run: bool):
    now = utcnow()
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    event = Event(
        resource.api,
        {
            "metadata": {
                "namespace": resource.namespace,
                "generateName": "kube-janitor-",
            },
            "type": "Normal",
            "count": 1,
            "firstTimestamp": timestamp,
            "lastTimestamp": timestamp,
            "reason": reason,
            "involvedObject": {
                "apiVersion": resource.version,
                "name": resource.name,
                "namespace": resource.namespace,
                "kind": resource.kind,
                "resourceVersion": resource.metadata.get("resourceVersion"),
                # https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#uids
                "uid": resource.metadata.get("uid"),
            },
            "message": message,
            "source": {"component": "kube-janitor"},
        },
    )
    if not dry_run:
        try:
            event.create()
        except Exception as e:
            logger.error(f"Could not create event {event.obj}: {e}")


def delete(resource, wait_after_delete: int, dry_run: bool):
    if dry_run:
        logger.info(
            f"**DRY-RUN**: would delete {resource.kind} {resource.namespace}/{resource.name}"
        )
    else:
        logger.info(
            f'Deleting {resource.kind} {resource.namespace or ""}{"/" if resource.namespace else ""}{resource.name}..'
        )
        try:
            # force cascading delete also for older objects (e.g. extensions/v1beta1)
            # see https://kubernetes.io/docs/concepts/workloads/controllers/garbage-collection/#setting-the-cascading-deletion-policy
            # use "Background" instead of "Foreground" to fix CRD deletion, see https://codeberg.org/hjacobs/kube-janitor/issues/47
            resource.delete(propagation_policy="Background")
        except Exception as e:
            logger.error(
                f"Could not delete {resource.kind} {resource.namespace}/{resource.name}: {e}"
            )
        if wait_after_delete > 0:
            logger.debug(f"Waiting {wait_after_delete}s after deletion")
            time.sleep(wait_after_delete)


def handle_resource_on_ttl(
    resource,
    rules,
    delete_notification: int,
    deployment_time_annotation: Optional[str] = None,
    resource_context_hook: Optional[Callable[[APIObject, dict], Dict[str, Any]]] = None,
    cache: Dict[str, Any] = None,
    wait_after_delete: int = 0,
    dry_run: bool = False,
):
    counter = {"resources-processed": 1}

    ttl = resource.annotations.get(TTL_ANNOTATION)
    if ttl:
        reason = f"annotation {TTL_ANNOTATION} is set"
    else:
        context = get_resource_context(resource, resource_context_hook, cache)
        for rule in rules:
            if rule.matches(resource, context):
                logger.debug(
                    f"Rule {rule.id} applies {rule.ttl} TTL to {resource.kind} {resource.namespace}/{resource.name}"
                )
                ttl = rule.ttl
                reason = f"rule {rule.id} matches"
                counter[f"rule-{rule.id}-matches"] = 1
                # first rule which matches
                break
    if ttl:
        try:
            ttl_seconds = parse_ttl(ttl)
        except ValueError as e:
            logger.info(f"Ignoring invalid TTL on {resource.kind} {resource.name}: {e}")
        else:
            if ttl_seconds > 0:
                counter[f"{resource.endpoint}-with-ttl"] = 1
                deployment_time = get_deployment_time(
                    resource, deployment_time_annotation
                )
                age = utcnow() - deployment_time
                age_formatted = format_duration(int(age.total_seconds()))
                logger.debug(
                    f"{resource.kind} {resource.name} with {ttl} TTL is {age_formatted} old"
                )
                if age.total_seconds() > ttl_seconds:
                    message = f"{resource.kind} {resource.name} with {ttl} TTL is {age_formatted} old and will be deleted ({reason})"
                    logger.info(message)
                    if delete_notification:
                        create_event(
                            resource, message, "TimeToLiveExpired", dry_run=dry_run
                        )
                    delete(
                        resource, wait_after_delete=wait_after_delete, dry_run=dry_run
                    )
                    counter[f"{resource.endpoint}-deleted"] = 1
                elif delete_notification:
                    expiry_time = deployment_time + datetime.timedelta(
                        seconds=ttl_seconds
                    )
                    notification_time = get_delete_notification_time(
                        expiry_time, delete_notification
                    )
                    if utcnow() > notification_time and not was_notified(resource):
                        send_delete_notification(
                            resource, reason, expiry_time, dry_run=dry_run
                        )

    return counter


def handle_resource_on_expiry(
    resource, rules, delete_notification: int, wait_after_delete: int, dry_run: bool
):
    counter = {}

    expiry = resource.annotations.get(EXPIRY_ANNOTATION)
    if expiry:
        reason = f"annotation {EXPIRY_ANNOTATION} is set"
        try:
            expiry_timestamp = parse_expiry(expiry)
        except ValueError as e:
            logger.info(
                f"Ignoring invalid expiry date on {resource.kind} {resource.name}: {e}"
            )
        else:
            counter[f"{resource.endpoint}-with-expiry"] = 1
            now = utcnow()
            if now > expiry_timestamp:
                message = f"{resource.kind} {resource.name} expired on {expiry} and will be deleted ({reason})"
                logger.info(message)
                if delete_notification:
                    create_event(
                        resource, message, "ExpiryTimeReached", dry_run=dry_run
                    )
                delete(resource, wait_after_delete=wait_after_delete, dry_run=dry_run)
                counter[f"{resource.endpoint}-deleted"] = 1
            else:
                logging.debug(
                    f"{resource.kind} {resource.name} will expire on {expiry}"
                )
                if delete_notification:
                    notification_time = get_delete_notification_time(
                        expiry_timestamp, delete_notification
                    )
                    if now > notification_time and not was_notified(resource):
                        send_delete_notification(
                            resource, reason, expiry_timestamp, dry_run=dry_run
                        )

    return counter


def clean_up(
    api,
    include_resources: frozenset,
    exclude_resources: frozenset,
    include_namespaces: frozenset,
    exclude_namespaces: frozenset,
    rules: list,
    delete_notification: int,
    deployment_time_annotation: Optional[str] = None,
    resource_context_hook: Optional[Callable[[APIObject, dict], Dict[str, Any]]] = None,
    wait_after_delete: int = 0,
    dry_run: bool = False,
):

    counter: Counter = Counter()
    cache: Dict[str, Any] = {}

    if (
        "all" in include_resources and "namespace" not in exclude_resources
    ) or "namespace" in include_resources:
        namespaces = []

        if "all" in include_namespaces:
            namespaces.extend(list(Namespace.objects(api)))
        else:
            for namespace_name in include_namespaces:
                namespace = Namespace.objects(api).get(name=namespace_name)
                namespaces.append(namespace)

        for namespace in namespaces:
            if matches_resource_filter(
                namespace,
                include_resources,
                exclude_resources,
                include_namespaces,
                exclude_namespaces,
            ):
                counter.update(
                    handle_resource_on_ttl(
                        namespace,
                        rules,
                        delete_notification,
                        deployment_time_annotation,
                        resource_context_hook,
                        cache,
                        wait_after_delete,
                        dry_run,
                    )
                )
                counter.update(
                    handle_resource_on_expiry(
                        namespace,
                        rules,
                        delete_notification,
                        wait_after_delete,
                        dry_run,
                    )
                )
            else:
                logger.debug(f"Skipping {namespace.kind} {namespace}")
    else:
        logger.debug("Skipping resource type namespace")

    already_seen: set = set()

    filtered_resources = []

    resource_types = get_namespaced_resource_types(api)
    for _type in resource_types:
        if (
            "all" in include_resources and _type.endpoint not in exclude_resources
        ) or _type.endpoint in include_resources:
            try:
                resources = []
                if "all" in include_namespaces:
                    resources.extend(list(_type.objects(api, namespace=pykube.all)))
                else:
                    for namespace_name in include_namespaces:
                        resources.extend(
                            list(_type.objects(api, namespace=namespace_name))
                        )

                for resource in resources:
                    # objects might be available via multiple API versions (e.g. deployments appear as extensions/v1beta1 and apps/v1)
                    # => process them only once
                    object_id = (resource.kind, resource.namespace, resource.name)
                    if object_id in already_seen:
                        continue
                    already_seen.add(object_id)
                    if matches_resource_filter(
                        resource,
                        include_resources,
                        exclude_resources,
                        include_namespaces,
                        exclude_namespaces,
                    ):
                        filtered_resources.append(resource)
                    else:
                        logger.debug(
                            f"Skipping {resource.kind} {resource.namespace}/{resource.name}"
                        )
            except Exception as e:
                logger.error(f"Could not list {_type.kind} objects: {e}")
        else:
            logger.debug(f"Skipping resource type {_type.endpoint}")

    for resource in filtered_resources:
        counter.update(
            handle_resource_on_ttl(
                resource,
                rules,
                delete_notification,
                deployment_time_annotation,
                resource_context_hook,
                cache,
                wait_after_delete,
                dry_run,
            )
        )
        counter.update(
            handle_resource_on_expiry(
                resource, rules, delete_notification, wait_after_delete, dry_run
            )
        )
    stats = ", ".join([f"{k}={v}" for k, v in counter.items()])
    logger.info(f"Clean up run completed: {stats}")
    return counter
