import kopf
import kubernetes
from kubernetes import client
from kubernetes.client.rest import ApiException
import logging
import os
from typing import Dict, Any, List

GROUP = "ai.redhat.example.com"
VERSION = "v1alpha1"
PLURAL = "llmdeployments"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_labels(name: str) -> Dict[str, str]:
    return {
        "app.kubernetes.io/name": "llm-deployment",
        "app.kubernetes.io/managed-by": "llm-kubernetes-operator",
        "app.kubernetes.io/instance": name,
    }


def build_env_list(env_dict: Dict[str, str]) -> List[client.V1EnvVar]:
    env_vars = []
    for k, v in (env_dict or {}).items():
        env_vars.append(client.V1EnvVar(name=k, value=str(v)))
    return env_vars


def build_container(spec: Dict[str, Any], name: str) -> client.V1Container:
    image = spec.get("image", "python:3.11-slim")
    port = spec.get("port", 8000)
    model_name = spec.get("modelName", "demo-model")
    env = spec.get("env", {})
    resources = spec.get("resources", {})

    env = {
        **env,
        "MODEL_NAME": model_name,
        "PORT": str(port),
    }

    container = client.V1Container(
        name="llm-server",
        image=image,
        image_pull_policy="IfNotPresent",
        ports=[client.V1ContainerPort(container_port=port)],
        env=build_env_list(env),
    )

    if resources:
        container.resources = client.V1ResourceRequirements(
            requests=resources.get("requests"),
            limits=resources.get("limits"),
        )

    # Optional probes
    container.readiness_probe = client.V1Probe(
        http_get=client.V1HTTPGetAction(path="/health", port=port),
        initial_delay_seconds=5,
        period_seconds=10,
    )
    container.liveness_probe = client.V1Probe(
        http_get=client.V1HTTPGetAction(path="/health", port=port),
        initial_delay_seconds=10,
        period_seconds=15,
    )

    return container


def build_deployment_body(name: str, namespace: str, spec: Dict[str, Any]) -> client.V1Deployment:
    labels = get_labels(name)
    replicas = spec.get("replicas", 1)
    container = build_container(spec, name)

    pod_spec = client.V1PodSpec(
        containers=[container],
        service_account_name="llm-operator-controller-sa",
    )

    # Optional node selectors / tolerations
    if spec.get("nodeSelector"):
        pod_spec.node_selector = spec["nodeSelector"]

    if spec.get("tolerations"):
        pod_spec.tolerations = [
            client.V1Toleration(**tol) for tol in spec["tolerations"]
        ]

    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=pod_spec,
    )

    deployment_spec = client.V1DeploymentSpec(
        replicas=replicas,
        selector=client.V1LabelSelector(match_labels=labels),
        template=template,
    )

    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
        ),
        spec=deployment_spec,
    )


def build_service_body(name: str, namespace: str, spec: Dict[str, Any]) -> client.V1Service:
    labels = get_labels(name)
    port = spec.get("port", 8000)

    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
        ),
        spec=client.V1ServiceSpec(
            selector=labels,
            ports=[
                client.V1ServicePort(
                    name="http",
                    port=port,
                    target_port=port,
                )
            ],
        ),
    )


def build_configmap_body(name: str, namespace: str, spec: Dict[str, Any]) -> client.V1ConfigMap:
    labels = get_labels(name)

    data = {
        "modelName": str(spec.get("modelName", "demo-model")),
        "routingPolicy": str(spec.get("routingPolicy", "round_robin")),
        "maxConcurrency": str(spec.get("maxConcurrency", 8)),
        "tensorParallelism": str(spec.get("tensorParallelism", 1)),
    }

    return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=f"{name}-config",
            namespace=namespace,
            labels=labels,
        ),
        data=data,
    )


def build_route_manifest(name: str, namespace: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    port = spec.get("port", 8000)
    labels = get_labels(name)

    return {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "to": {
                "kind": "Service",
                "name": name,
            },
            "port": {
                "targetPort": "http"
            },
            "tls": {
                "termination": "edge"
            }
        }
    }


def ensure_configmap(core_api: client.CoreV1Api, name: str, namespace: str, spec: Dict[str, Any]):
    body = build_configmap_body(name, namespace, spec)
    try:
        core_api.read_namespaced_config_map(name=f"{name}-config", namespace=namespace)
        core_api.replace_namespaced_config_map(name=f"{name}-config", namespace=namespace, body=body)
        logger.info("Updated ConfigMap %s/%s-config", namespace, name)
    except ApiException as e:
        if e.status == 404:
            core_api.create_namespaced_config_map(namespace=namespace, body=body)
            logger.info("Created ConfigMap %s/%s-config", namespace, name)
        else:
            raise


def ensure_service(core_api: client.CoreV1Api, name: str, namespace: str, spec: Dict[str, Any]):
    body = build_service_body(name, namespace, spec)
    try:
        core_api.read_namespaced_service(name=name, namespace=namespace)
        core_api.replace_namespaced_service(name=name, namespace=namespace, body=body)
        logger.info("Updated Service %s/%s", namespace, name)
    except ApiException as e:
        if e.status == 404:
            core_api.create_namespaced_service(namespace=namespace, body=body)
            logger.info("Created Service %s/%s", namespace, name)
        else:
            raise


def ensure_deployment(apps_api: client.AppsV1Api, name: str, namespace: str, spec: Dict[str, Any]):
    body = build_deployment_body(name, namespace, spec)
    try:
        apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        apps_api.replace_namespaced_deployment(name=name, namespace=namespace, body=body)
        logger.info("Updated Deployment %s/%s", namespace, name)
    except ApiException as e:
        if e.status == 404:
            apps_api.create_namespaced_deployment(namespace=namespace, body=body)
            logger.info("Created Deployment %s/%s", namespace, name)
        else:
            raise


def ensure_route(custom_api: client.CustomObjectsApi, name: str, namespace: str, spec: Dict[str, Any]):
    route_enabled = spec.get("route", {}).get("enabled", True)
    if not route_enabled:
        return None

    body = build_route_manifest(name, namespace, spec)

    try:
        existing = custom_api.get_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
        )
        custom_api.replace_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
            body=body,
        )
        logger.info("Updated Route %s/%s", namespace, name)
        return existing
    except ApiException as e:
        if e.status == 404:
            created = custom_api.create_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                body=body,
            )
            logger.info("Created Route %s/%s", namespace, name)
            return created
        # Route API may not exist on plain Kubernetes
        if e.status in (403, 404):
            logger.warning("Route API unavailable in namespace=%s name=%s", namespace, name)
            return None
        raise
    except Exception as e:
        logger.warning("Skipping Route creation for %s/%s: %s", namespace, name, str(e))
        return None


def get_route_host(custom_api: client.CustomObjectsApi, name: str, namespace: str):
    try:
        route = custom_api.get_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
        )
        return route.get("spec", {}).get("host")
    except Exception:
        return None


def patch_status(namespace: str, name: str, status: Dict[str, Any]):
    api = client.CustomObjectsApi()
    body = {"status": status}
    api.patch_namespaced_custom_object_status(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=PLURAL,
        name=name,
        body=body,
    )


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    kubernetes.config.load_incluster_config() if os.getenv("KUBERNETES_SERVICE_HOST") else kubernetes.config.load_kube_config()
    settings.persistence.finalizer = f"{GROUP}/finalizer"
    logger.info("Operator startup complete")


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def reconcile(spec, name, namespace, status, **kwargs):
    logger.info("Reconciling %s/%s", namespace, name)

    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()

    ensure_configmap(core_api, name, namespace, spec)
    ensure_service(core_api, name, namespace, spec)
    ensure_deployment(apps_api, name, namespace, spec)
    ensure_route(custom_api, name, namespace, spec)

    deployment = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
    ready_replicas = deployment.status.ready_replicas or 0
    desired_replicas = deployment.spec.replicas or 0
    route_host = get_route_host(custom_api, name, namespace)

    service_url = f"http://{name}.{namespace}.svc.cluster.local:{spec.get('port', 8000)}"
    external_url = f"https://{route_host}" if route_host else None

    new_status = {
        "phase": "Ready" if ready_replicas == desired_replicas and desired_replicas > 0 else "Progressing",
        "readyReplicas": ready_replicas,
        "desiredReplicas": desired_replicas,
        "serviceUrl": service_url,
        "routeHost": route_host,
        "externalUrl": external_url,
        "observedGeneration": kwargs["body"]["metadata"].get("generation", 1),
    }

    patch_status(namespace, name, new_status)

    return {"message": f"Reconciled {name}", "status": new_status}


@kopf.on.delete(GROUP, VERSION, PLURAL)
def delete_fn(spec, name, namespace, **kwargs):
    logger.info("Deleting owned resources for %s/%s", namespace, name)

    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()

    for deleter, resource_name in [
        (lambda: apps_api.delete_namespaced_deployment(name=name, namespace=namespace), f"Deployment/{name}"),
        (lambda: core_api.delete_namespaced_service(name=name, namespace=namespace), f"Service/{name}"),
        (lambda: core_api.delete_namespaced_config_map(name=f'{name}-config', namespace=namespace), f"ConfigMap/{name}-config"),
    ]:
        try:
            deleter()
            logger.info("Deleted %s", resource_name)
        except ApiException as e:
            if e.status != 404:
                raise

    try:
        custom_api.delete_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
        )
        logger.info("Deleted Route/%s", name)
    except Exception:
        pass