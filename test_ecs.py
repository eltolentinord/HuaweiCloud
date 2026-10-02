# coding: utf-8
"""
Consulta de inventario de servidores ECS en Huawei Cloud (solo lectura).

Requisitos:
    huaweicloudsdkcore
    huaweicloudsdkecs

Uso:
    python test_ecs.py
    python test_ecs.py --raw-json
    python test_ecs.py --server-id ID_DE_LA_ECS

Credenciales (variables de entorno, ver .env.example; config.json como respaldo):
    HUAWEI_AK, HUAWEI_SK, HUAWEI_PROJECT_ID, HUAWEI_REGION
"""

import argparse
import json
import re
import sys
from pathlib import Path

from huaweicloudsdkcore.exceptions import exceptions

from collectors.ecs import EcsCollector
from core.clients import ClientFactory
from core.credentials import (
    DEFAULT_REGION,
    ENV_AK,
    ENV_PROJECT_ID,
    ENV_REGION,
    ENV_SK,
    EnvCredentialProvider,
)
from core.errors import mask_project_id, safe_message
from core.serialization import make_serializable, to_text

CARPETA_SALIDA = Path("output")
ARCHIVO_CONFIG = Path(__file__).resolve().parent / "config.json"


# ==========================================================
# UTILIDADES
# ==========================================================

def texto(valor, defecto="No disponible"):
    """Convierte un valor a texto legible, sin asumir su tipo."""
    return to_text(valor, defecto)


def sanear_nombre_archivo(nombre):
    """Elimina caracteres no válidos para nombres de archivo en Windows."""
    if not nombre:
        return "ecs"
    saneado = re.sub(r'[<>:"/\|?*\x00-\x1f]', "_", str(nombre))
    saneado = saneado.strip(" .")
    if not saneado:
        return "ecs"
    return saneado[:80]


def cargar_config_local():
    """Lee config.json local como respaldo cuando faltan variables de entorno."""
    if not ARCHIVO_CONFIG.exists():
        return {}
    try:
        with open(ARCHIVO_CONFIG, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
        if isinstance(datos, dict):
            return datos
    except Exception as error:
        print(f"AVISO: no se pudo leer {ARCHIVO_CONFIG}: {type(error).__name__}")
    return {}


def pedir_configuracion(nombre):
    print(f"ERROR: falta la variable de entorno {nombre}")
    print("Configúrala en PowerShell con:")
    print(f'    $env:{nombre} = "TU_VALOR"')
    sys.exit(1)


# ==========================================================
# CONSULTA
# ==========================================================

def listar_todas_las_ecs(client, server_id=None):
    """Todas las ECS (paginación por número de página del collector ECS)."""
    filtros = {"server_id": server_id} if server_id else {}
    servidores = EcsCollector().fetch(client, **filtros)
    if server_id:
        servidores = [s for s in servidores if s.get("id") == server_id]
    return servidores


# ==========================================================
# PRESENTACIÓN
# ==========================================================

def mostrar_ecs(numero, data):
    print("\n" + "=" * 70)
    print(f"ECS #{numero}")
    print("=" * 70)

    print(f"Nombre          : {texto(data.get('name'))}")
    print(f"ID              : {texto(data.get('id'))}")
    print(f"Estado          : {texto(data.get('status'))}")
    print(f"Creado          : {texto(data.get('created'))}")
    print(f"Actualizado     : {texto(data.get('updated'))}")
    print(f"Tenant ID       : {texto(data.get('tenant_id'))}")
    print(f"Usuario ID      : {texto(data.get('user_id'))}")
    print(f"Descripción     : {texto(data.get('description'))}")
    print(f"KeyPair         : {texto(data.get('key_name'))}")
    print(f"P. de empresa   : {texto(data.get('enterprise_project_id'))}")

    print("\nUBICACIÓN Y ESTADO")
    print("-" * 70)
    print(f"Zona AZ         : {texto(data.get('OS-EXT-AZ:availability_zone'))}")
    print(f"Power State     : {texto(data.get('OS-EXT-STS:power_state'))}")
    print(f"Task State      : {texto(data.get('OS-EXT-STS:task_state'))}")
    print(f"VM State        : {texto(data.get('OS-EXT-STS:vm_state'))}")

    print("\nFLAVOR")
    print("-" * 70)
    flavor = data.get("flavor")
    if isinstance(flavor, dict):
        print(f"Flavor ID       : {texto(flavor.get('id'))}")
        print(f"Flavor nombre   : {texto(flavor.get('name'))}")
        print(f"vCPU            : {texto(flavor.get('vcpus'))}")
        print(f"RAM MB          : {texto(flavor.get('ram'))}")
    else:
        print(f"Flavor          : {texto(flavor)}")

    print("\nIMAGEN")
    print("-" * 70)
    image = data.get("image")
    if isinstance(image, dict):
        print(f"Image ID        : {texto(image.get('id'))}")
    else:
        print(f"Imagen          : {texto(image)}")

    print("\nSECURITY GROUPS")
    print("-" * 70)
    security_groups = data.get("security_groups")
    if isinstance(security_groups, list) and security_groups:
        for sg in security_groups:
            if isinstance(sg, dict):
                print(f"Nombre          : {texto(sg.get('name'))}")
                print(f"ID              : {texto(sg.get('id'))}")
            else:
                print(texto(sg))
            print("-" * 35)
    else:
        print("No se encontraron Security Groups.")

    print("\nREDES E IP")
    print("-" * 70)
    addresses = data.get("addresses")
    ips_privadas = []
    ips_publicas = []
    macs = []

    if isinstance(addresses, dict) and addresses:
        for network_name, ip_list in addresses.items():
            print(f"\nRed             : {network_name}")
            if isinstance(ip_list, list):
                for ip_data in ip_list:
                    if not isinstance(ip_data, dict):
                        ip_data = make_serializable(ip_data)
                    if not isinstance(ip_data, dict):
                        print(texto(ip_data))
                        continue

                    direccion = ip_data.get("addr")
                    tipo = ip_data.get("OS-EXT-IPS:type")
                    mac = ip_data.get("OS-EXT-IPS-MAC:mac_addr")

                    if tipo == "floating" and direccion:
                        ips_publicas.append(direccion)
                    elif direccion:
                        ips_privadas.append(direccion)
                    if mac:
                        macs.append(mac)

                    print(f"IP              : {texto(direccion)}")
                    print(f"Versión         : {texto(ip_data.get('version'))}")
                    print(f"Tipo            : {texto(tipo)}")
                    print(f"MAC             : {texto(mac)}")
                    print("-" * 35)
    else:
        print("No se encontraron redes.")

    print(f"\nIP privada      : {texto(ips_privadas)}")
    print(f"IP pública      : {texto(ips_publicas)}")
    print(f"MAC address     : {texto(macs)}")

    print("\nDISCOS")
    print("-" * 70)
    volumes = data.get("os-extended-volumes:volumes_attached")
    if isinstance(volumes, list) and volumes:
        for volume in volumes:
            if not isinstance(volume, dict):
                volume = make_serializable(volume)
            if isinstance(volume, dict):
                print(f"Volume ID       : {texto(volume.get('id'))}")
                print(f"Boot Index      : {texto(volume.get('bootIndex'))}")
                print(f"Device          : {texto(volume.get('device'))}")
                print(f"Delete al borrar: {texto(volume.get('delete_on_termination'))}")
            else:
                print(texto(volume))
            print("-" * 35)
    else:
        print("No se encontraron discos.")

    print("\nTAGS")
    print("-" * 70)
    tags = data.get("tags")
    if isinstance(tags, list) and tags:
        for tag in tags:
            print(f"- {tag}")
    else:
        print("Sin tags.")

    print("\nMETADATA")
    print("-" * 70)
    metadata = data.get("metadata")
    if isinstance(metadata, dict) and metadata:
        for clave, valor in metadata.items():
            print(f"{clave}: {valor}")
    else:
        print("Sin metadata.")


# ==========================================================
# PRINCIPAL
# ==========================================================

def main():
    parser = argparse.ArgumentParser(
        description="Consulta inventario de ECS en Huawei Cloud (solo lectura)."
    )
    parser.add_argument(
        "--raw-json",
        action="store_true",
        help="Guarda un JSON completo por cada ECS en la carpeta output.",
    )
    parser.add_argument(
        "--server-id",
        help="Filtra y muestra solamente la ECS con ese ID.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("INICIANDO CONSULTA DE ECS")
    print("=" * 70)

    proveedor = EnvCredentialProvider(fallback=cargar_config_local())
    ak = proveedor.value(ENV_AK)
    sk = proveedor.value(ENV_SK)
    project_id = proveedor.value(ENV_PROJECT_ID)
    region = proveedor.value(ENV_REGION, DEFAULT_REGION)

    if not ak:
        pedir_configuracion(ENV_AK)
    if not sk:
        pedir_configuracion(ENV_SK)
    if not project_id:
        pedir_configuracion(ENV_PROJECT_ID)

    print(f"Región          : {region}")
    print(f"Project ID      : {mask_project_id(project_id)}")
    print("Credenciales detectadas")
    print("Conectando con Huawei Cloud...")

    fabrica = ClientFactory(proveedor.get_credentials())
    client = fabrica.create(EcsCollector.client_cls, EcsCollector.region_cls,
                            EcsCollector.endpoint_prefix, region, project_id)

    try:
        print("Consultando ECS...")
        servidores = listar_todas_las_ecs(client, server_id=args.server_id)
        print(f"Cantidad de ECS encontradas: {len(servidores)}")

        if not servidores:
            if args.server_id:
                print(f"No se encontró ninguna ECS con ID: {args.server_id}")
            else:
                print(
                    "No hay ECS en este Project ID y región, "
                    "o el usuario no tiene permiso para verlas."
                )
            return

        if args.raw_json:
            CARPETA_SALIDA.mkdir(parents=True, exist_ok=True)

        for numero, server in enumerate(servidores, start=1):
            data = server if isinstance(server, dict) else {"value": make_serializable(server)}

            if args.raw_json:
                nombre = sanear_nombre_archivo(data.get("name") or "ecs")
                server_id_data = sanear_nombre_archivo(data.get("id") or str(numero))
                ruta = CARPETA_SALIDA / f"{nombre}-{server_id_data}.json"
                with open(ruta, "w", encoding="utf-8") as archivo:
                    json.dump(data, archivo, ensure_ascii=False, indent=2)
                print(f"Guardado: {ruta}")

            mostrar_ecs(numero, data)

        print("\n" + "=" * 70)
        print("Consulta finalizada correctamente")
        print("=" * 70)

    except exceptions.ClientRequestException as error:
        print("\nERROR DE HUAWEI CLOUD")
        print("=" * 70)
        print(f"HTTP Status     : {error.status_code}")
        print(f"Request ID      : {error.request_id}")
        print(f"Error Code      : {error.error_code}")
        print(f"Error Message   : {error.error_msg}")

    except Exception as error:
        print("\nERROR GENERAL")
        print("=" * 70)
        print(f"Tipo            : {type(error).__name__}")
        print(f"Mensaje         : {safe_message(error, (ak, sk))}")


if __name__ == "__main__":
    main()
