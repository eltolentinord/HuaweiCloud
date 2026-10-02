# coding: utf-8
"""Prueba manual: lista los flavors de ECS con endpoints explícitos (p. ej. Huawei Cloud Europa).

Credenciales y endpoints SOLO por variables de entorno (ver .env.example):
    HUAWEI_AK, HUAWEI_SK, HUAWEI_PROJECT_ID, HUAWEI_REGION,
    HUAWEI_IAM_ENDPOINT, HUAWEI_ECS_ENDPOINT
"""

import os
import sys

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region as CoreRegion
from huaweicloudsdkecs.v2 import EcsClient, ListFlavorsRequest

DEFAULT_REGION = "ap-southeast-3"
DEFAULT_IAM_ENDPOINT = "https://iam.eu-west-101.myhuaweicloud.eu"
DEFAULT_ECS_ENDPOINT = "https://ecs.ap-southeast-3.myhuaweicloud.eu"


def requerida(nombre):
    valor = (os.environ.get(nombre) or "").strip()
    if not valor:
        print(f"ERROR: falta la variable de entorno {nombre}")
        sys.exit(1)
    return valor


if __name__ == "__main__":
    ak = requerida("HUAWEI_AK")
    sk = requerida("HUAWEI_SK")
    project_id = requerida("HUAWEI_PROJECT_ID")
    region = os.environ.get("HUAWEI_REGION") or DEFAULT_REGION
    iam_endpoint = os.environ.get("HUAWEI_IAM_ENDPOINT") or DEFAULT_IAM_ENDPOINT
    ecs_endpoint = os.environ.get("HUAWEI_ECS_ENDPOINT") or DEFAULT_ECS_ENDPOINT

    credentials = BasicCredentials(ak, sk, project_id).with_iam_endpoint(iam_endpoint)

    client = (
        EcsClient.new_builder()
        .with_credentials(credentials)
        .with_region(CoreRegion(id=region, endpoint=ecs_endpoint))
        .build()
    )

    try:
        request = ListFlavorsRequest()
        request.limit = 100

        response = client.list_flavors(request)

        for flavor in response.flavors:
            ram_gib = flavor.ram / 1024 if flavor.ram else 0

            print(
                f"ID: {flavor.id} | "
                f"Nombre: {flavor.name} | "
                f"vCPU: {flavor.vcpus} | "
                f"RAM: {ram_gib:g} GiB"
            )

    except exceptions.ClientRequestException as error:
        print("HTTP:", error.status_code)
        print("Request ID:", error.request_id)
        print("Código:", error.error_code)
        print("Mensaje:", error.error_msg)
