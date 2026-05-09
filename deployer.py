import argparse
import sys
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def load_spec(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def labels(name):
    return {
        "app.kubernetes.io/name": "llm-deployer",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/managed-by": "llm-deployer",
    }


def env_list(env_dict):
    envs = []
    for k, v in (env_dict or {}).items():
        envs.append(client.V1EnvVar(name=k, value=str(v)))
    return envs


def build_configmap(name, namespace, spec):
    return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=f"{name}-config",
            namespace=namespace,
            labels=labels(name),
        ),
        data={
            "modelName": str(spec.get("modelName", "demo-model")),
            "routingPolicy": str(spec.get("routingPolicy", "round_robin")),
            "maxConcurrency": str(spec.get("maxConcurrency", 8)),
            "tensorParallelism": str(spec.get("tensorParallelism", 1)),
        },
    )


def build_deployment(name, namespace, spec):
    port = spec.get("port", 8080)
    image = spec["image"]
    replicas = spec.get("replicas", 1)
    model_name = spec.get("modelName", "demo-model")
    env = {
        **(spec.get("env", {}) or {}),
        "MODEL_NAME": model_name,
        "PORT": str(port),
    }
    resources = spec.get("resources", {})

    container = client.V1Container(
        name="llm-server",
        image=image,
        image_pull_policy="IfNotPresent",
        ports=[client.V1ContainerPort(container_port=port)],
        env=env_list(env),
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/", port=port),
            initial_delay_seconds=3,
            period_seconds=10,
        ),
        liveness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/", port=port),
            initial_delay_seconds=5,
            period_seconds=15,
        ),
    )

    if resources:
        container.resources = client.V1ResourceRequirements(
            requests=resources.get("requests"),
            limits=resources.get("limits"),
        )

    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels(name),
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels=labels(name)),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels(name)),
                spec=client.V1PodSpec(containers=[container]),
            ),
        ),
    )


def build_service(name, namespace, spec):
    port = spec.get("port", 8080)
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels(name),
        ),
        spec=client.V1ServiceSpec(
            selector=labels(name),
            ports=[
                client.V1ServicePort(
                    name="http",
                    port=port,
                    target_port=port,
                )
            ],
        ),
    )


def build_route(name, namespace):
    return {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels(name),
        },
        "spec": {
            "to": {"kind": "Service", "name": name},
            "port": {"targetPort": "http"},
            "tls": {"termination": "edge"},
        },
    }


def apply_configmap(core_api, name, namespace, spec):
    body = build_configmap(name, namespace, spec)
    try:
        core_api.read_namespaced_config_map(name=f"{name}-config", namespace=namespace)
        core_api.replace_namespaced_config_map(name=f"{name}-config", namespace=namespace, body=body)
        print(f"updated ConfigMap/{name}-config")
    except ApiException as e:
        if e.status == 404:
            core_api.create_namespaced_config_map(namespace=namespace, body=body)
            print(f"created ConfigMap/{name}-config")
        else:
            raise


def apply_deployment(apps_api, name, namespace, spec):
    body = build_deployment(name, namespace, spec)
    try:
        apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        apps_api.replace_namespaced_deployment(name=name, namespace=namespace, body=body)
        print(f"updated Deployment/{name}")
    except ApiException as e:
        if e.status == 404:
            apps_api.create_namespaced_deployment(namespace=namespace, body=body)
            print(f"created Deployment/{name}")
        else:
            raise


def apply_service(core_api, name, namespace, spec):
    body = build_service(name, namespace, spec)
    try:
        existing = core_api.read_namespaced_service(name=name, namespace=namespace)
        body.spec.cluster_ip = existing.spec.cluster_ip
        core_api.replace_namespaced_service(name=name, namespace=namespace, body=body)
        print(f"updated Service/{name}")
    except ApiException as e:
        if e.status == 404:
            core_api.create_namespaced_service(namespace=namespace, body=body)
            print(f"created Service/{name}")
        else:
            raise


def apply_route(custom_api, name, namespace):
    body = build_route(name, namespace)
    try:
        existing = custom_api.get_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
        )
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        custom_api.replace_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
            body=body,
        )
        print(f"updated Route/{name}")
    except ApiException as e:
        if e.status == 404:
            custom_api.create_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                body=body,
            )
            print(f"created Route/{name}")
        else:
            raise


def delete_resource(fn, kind, name):
    try:
        fn()
        print(f"deleted {kind}/{name}")
    except ApiException as e:
        if e.status != 404:
            raise


def show_status(apps_api, custom_api, name, namespace):
    try:
        dep = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        ready = dep.status.ready_replicas or 0
        desired = dep.spec.replicas or 0
        print(f"deployment: {ready}/{desired} ready")
    except ApiException:
        print("deployment: not found")

    try:
        route = custom_api.get_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=name,
        )
        host = route.get("spec", {}).get("host")
        print(f"route: https://{host}" if host else "route: created, host pending")
    except ApiException:
        print("route: not found")


def main():
    parser = argparse.ArgumentParser(description="OpenShift Inference Control Plane Prototype")
    parser.add_argument("action", choices=["apply", "delete", "status"])
    parser.add_argument("--file", required=True)
    args = parser.parse_args()

    config.load_kube_config()

    doc = load_spec(args.file)
    metadata = doc.get("metadata", {})
    spec = doc.get("spec", {})
    name = metadata.get("name")
    namespace = metadata.get("namespace", "default")

    if not name:
        print("metadata.name is required")
        sys.exit(1)

    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()

    if args.action == "apply":
        apply_configmap(core_api, name, namespace, spec)
        apply_deployment(apps_api, name, namespace, spec)
        apply_service(core_api, name, namespace, spec)
        apply_route(custom_api, name, namespace)
        show_status(apps_api, custom_api, name, namespace)
    elif args.action == "delete":
        delete_resource(
            lambda: custom_api.delete_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=name,
            ),
            "Route",
            name,
        )
        delete_resource(lambda: core_api.delete_namespaced_service(name=name, namespace=namespace), "Service", name)
        delete_resource(lambda: apps_api.delete_namespaced_deployment(name=name, namespace=namespace), "Deployment", name)
        delete_resource(
            lambda: core_api.delete_namespaced_config_map(name=f"{name}-config", namespace=namespace),
            "ConfigMap",
            f"{name}-config",
        )
    else:
        show_status(apps_api, custom_api, name, namespace)


if __name__ == "__main__":
    main()
