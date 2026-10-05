#!/usr/bin/env bash
#
# server-audit.sh
#
# Levantamiento rapido de un servidor Linux (cualquier distribucion:
# Debian/Ubuntu, RHEL/CentOS/Rocky/Alma, Fedora, SUSE, Alpine, Arch, etc.):
#   - Distro y version
#   - Motores de base de datos instalados/corriendo (MySQL, MariaDB,
#     PostgreSQL, MongoDB, Redis, SQLite)
#   - Sitios (vhosts) configurados en Apache y Nginx, con su estado
#   - Versiones de PHP instaladas y cual esta realmente en ejecucion
#     (CLI, FPM, modulo de Apache)
#
# Uso:
#   sudo ./server-audit.sh                       # muestra el reporte en pantalla
#   sudo ./server-audit.sh -o out.txt            # ademas lo guarda en un archivo de texto
#   sudo ./server-audit.sh -r reporte.html       # ademas genera un reporte HTML con
#                                                 # puntaje de hardening/actualizaciones,
#                                                 # graficos y recomendaciones
#   sudo ./server-audit.sh -m reporte.md         # ademas genera el mismo reporte en Markdown
#   sudo ./server-audit.sh -j reporte.json       # ademas exporta los resultados en JSON
#                                                 # (util para alimentar compare-audits.sh
#                                                 # y comparar varios servidores)
#
# Diseño: la deteccion se basa en binarios disponibles en PATH, unidades
# systemd (con fallback a "service"/init.d/pgrep) y en los propios
# comandos de introspeccion de cada servicio (apache2ctl -S / httpd -S,
# nginx -T), en vez de asumir rutas fijas de un solo empaquetador. Asi
# funciona igual con apt, dnf/yum, zypper, apk o pacman.
#
# Se debe ejecutar localmente en cada servidor. No requiere sudo para la
# mayoria de los checks, pero con privilegios de root algunos comandos
# (systemctl status, ss, nginx -T, apache2ctl -S) muestran mas detalle.

set -uo pipefail

# ------------------------------------------------------------------
# Opciones
# ------------------------------------------------------------------
OUTPUT_FILE=""
HTML_REPORT=""
MD_REPORT=""
JSON_REPORT=""
while getopts ":o:r:m:j:" opt; do
    case "$opt" in
        o) OUTPUT_FILE="$OPTARG" ;;
        r) HTML_REPORT="$OPTARG" ;;
        m) MD_REPORT="$OPTARG" ;;
        j) JSON_REPORT="$OPTARG" ;;
        *) echo "Uso: $0 [-o archivo_salida.txt] [-r reporte.html] [-m reporte.md] [-j reporte.json]"; exit 1 ;;
    esac
done

if [ -n "$OUTPUT_FILE" ]; then
    exec > >(tee "$OUTPUT_FILE") 2>&1
fi

# ------------------------------------------------------------------
# Colores (se desactivan si no hay terminal)
# ------------------------------------------------------------------
if [ -t 1 ]; then
    C_TITLE='\033[1;36m'
    C_OK='\033[1;32m'
    C_WARN='\033[1;33m'
    C_FAIL='\033[1;31m'
    C_INFO='\033[1;34m'
    C_OFF='\033[0m'
else
    C_TITLE=''; C_OK=''; C_WARN=''; C_FAIL=''; C_INFO=''; C_OFF=''
fi

section() {
    echo ""
    echo -e "${C_TITLE}== $1 ==${C_OFF}"
    echo "----------------------------------------------------------------"
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------------
# Helpers portables entre distros
# ------------------------------------------------------------------

# service_active <nombre>: intenta systemd, luego SysV/service, luego pgrep
service_active() {
    local svc="$1"
    if has_cmd systemctl; then
        systemctl is-active --quiet "$svc" 2>/dev/null && return 0
    fi
    if has_cmd service; then
        service "$svc" status >/dev/null 2>&1 && return 0
    fi
    if [ -x "/etc/init.d/$svc" ]; then
        /etc/init.d/"$svc" status >/dev/null 2>&1 && return 0
    fi
    if has_cmd pgrep; then
        pgrep -x "$svc" >/dev/null 2>&1 && return 0
    fi
    return 1
}

# list_services_matching <patron_glob>: nombres de unidades systemd que
# coincidan (ej: 'php*fpm*'), o scripts init.d como respaldo.
list_services_matching() {
    local pattern="$1"
    if has_cmd systemctl; then
        systemctl list-unit-files --no-legend --type=service 2>/dev/null \
            | awk '{print $1}' | grep -E "$pattern" | sort -u
        return
    fi
    if [ -d /etc/init.d ]; then
        ls /etc/init.d 2>/dev/null | grep -E "$pattern" | sort -u
    fi
}

# pkg_info <nombre_paquete>: version reportada por el gestor de paquetes
# disponible (best-effort; los nombres de paquete varian entre distros,
# por eso la deteccion principal NO depende de esto).
pkg_info() {
    local pkg="$1"
    if has_cmd dpkg-query; then
        dpkg-query -W -f='${Version}\n' "$pkg" 2>/dev/null && return 0
    fi
    if has_cmd rpm; then
        rpm -q --qf '%{VERSION}-%{RELEASE}\n' "$pkg" 2>/dev/null && return 0
    fi
    if has_cmd apk; then
        apk info -e "$pkg" >/dev/null 2>&1 && apk info "$pkg" 2>/dev/null | head -1 && return 0
    fi
    if has_cmd pacman; then
        pacman -Q "$pkg" 2>/dev/null | awk '{print $2}' && return 0
    fi
    return 1
}

# port_listening <puerto>: usa ss, si no existe usa netstat
port_listening() {
    local port="$1"
    if has_cmd ss; then
        ss -ltn 2>/dev/null | grep -q ":${port}[[:space:]]" && return 0
    elif has_cmd netstat; then
        netstat -ltn 2>/dev/null | grep -q ":${port}[[:space:]]" && return 0
    fi
    return 1
}

# get_ssh_value <directiva_sshd_-T> <directiva_sshd_config>: obtiene el
# valor efectivo de una directiva de SSH. Prioriza 'sshd -T' (config ya
# resuelta, incluye defaults y bloques Match) y si no esta disponible
# (no es root, o sshd no expone -T) cae a leer sshd_config directamente.
get_ssh_value() {
    local directive_lc="$1"
    local directive_cfg="$2"
    local val=""
    if [ -n "${SSHD_DUMP:-}" ]; then
        val=$(echo "$SSHD_DUMP" | grep -i "^${directive_lc} " | head -1 | awk '{print $2}')
    fi
    if [ -z "$val" ] && [ -f /etc/ssh/sshd_config ]; then
        val=$(grep -iE "^[[:space:]]*${directive_cfg}[[:space:]]+" /etc/ssh/sshd_config 2>/dev/null | tail -1 | awk '{print $2}')
    fi
    echo "${val:-no definido (valor por defecto de OpenSSH)}"
}

# ------------------------------------------------------------------
# Motor de puntaje de auditoria
#
# Cada control validado (hardening o actualizaciones) se registra con
# record_check, comparando el valor detectado contra la configuracion
# recomendada. Con esto se arma un puntaje objetivo por categoria y una
# lista de recomendaciones para los controles en WARN/FAIL, que luego
# se usan tanto en el resumen de consola como en el reporte HTML.
# ------------------------------------------------------------------
CHECK_IDS=()
declare -A CHECK_CAT=() CHECK_DESC=() CHECK_STATUS=() CHECK_PTS=() CHECK_MAX=() CHECK_REC=()

# record_check <id> <categoria> <descripcion> <OK|WARN|FAIL|INFO> <puntos> <max> <recomendacion>
record_check() {
    local id="$1"
    CHECK_IDS+=("$id")
    CHECK_CAT["$id"]="$2"
    CHECK_DESC["$id"]="$3"
    CHECK_STATUS["$id"]="$4"
    CHECK_PTS["$id"]="$5"
    CHECK_MAX["$id"]="$6"
    CHECK_REC["$id"]="${7:-}"
}

# category_score <categoria>: imprime "puntos_obtenidos puntos_max"
category_score() {
    local cat="$1" got=0 max=0 id
    for id in "${CHECK_IDS[@]}"; do
        if [ "${CHECK_CAT[$id]}" = "$cat" ]; then
            got=$(( got + CHECK_PTS[$id] ))
            max=$(( max + CHECK_MAX[$id] ))
        fi
    done
    echo "$got $max"
}

# text_bar <porcentaje> [ancho]: barra ASCII tipo [########....] 40%
text_bar() {
    local pct="$1" width="${2:-30}" filled empty
    filled=$(( pct * width / 100 ))
    [ "$filled" -gt "$width" ] && filled=$width
    [ "$filled" -lt 0 ] && filled=0
    empty=$(( width - filled ))
    printf '['
    [ "$filled" -gt 0 ] && printf '%*s' "$filled" '' | tr ' ' '#'
    [ "$empty" -gt 0 ] && printf '%*s' "$empty" '' | tr ' ' '.'
    printf '] %s%%' "$pct"
}

# ------------------------------------------------------------------
# Encabezado
# ------------------------------------------------------------------
echo "=================================================================="
echo " Server Audit - $(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)"
echo " Fecha: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================================="

# ------------------------------------------------------------------
# 1) Distro / SO (funciona en cualquier distro: usa /etc/os-release,
#    estandar de facto en systemd, con fallback a lsb_release)
# ------------------------------------------------------------------
section "Sistema Operativo"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "Distro    : ${PRETTY_NAME:-desconocida}"
    echo "ID        : ${ID:-desconocido}"
    echo "ID_LIKE   : ${ID_LIKE:-N/A}"
    echo "Version   : ${VERSION_ID:-desconocida}"
elif has_cmd lsb_release; then
    lsb_release -a 2>/dev/null
else
    echo "No se pudo determinar la distribucion (sin /etc/os-release ni lsb_release)"
fi
echo "Kernel    : $(uname -r)"
echo "Arch      : $(uname -m)"
echo "Gestor pkg: $(has_cmd apt && echo apt || true)$(has_cmd dnf && echo dnf || true)$(has_cmd yum && echo yum || true)$(has_cmd zypper && echo zypper || true)$(has_cmd apk && echo apk || true)$(has_cmd pacman && echo pacman || true)"

# ------------------------------------------------------------------
# 2) Bases de datos
# ------------------------------------------------------------------
section "Motores de Base de Datos"

# name | binarios candidatos (command -v) | patron de servicio (regex) | puerto por defecto | paquetes candidatos (best-effort)
DB_NAMES=("MySQL/MariaDB" "PostgreSQL" "MongoDB" "Redis" "SQLite3 (cliente)")
declare -A DB_BINS=(
    ["MySQL/MariaDB"]="mysqld mariadbd mysqld_safe"
    ["PostgreSQL"]="postgres postmaster"
    ["MongoDB"]="mongod"
    ["Redis"]="redis-server"
    ["SQLite3 (cliente)"]="sqlite3"
)
declare -A DB_SVC_PATTERN=(
    ["MySQL/MariaDB"]="^(mysql|mysqld|mariadb)\.service$"
    ["PostgreSQL"]="^postgresql(@.*)?\.service$"
    ["MongoDB"]="^mongod\.service$"
    ["Redis"]="^redis(-server)?\.service$"
    ["SQLite3 (cliente)"]=""
)
declare -A DB_PORTS=(
    ["MySQL/MariaDB"]="3306 3308"
    ["PostgreSQL"]="5432"
    ["MongoDB"]="27017"
    ["Redis"]="6379"
    ["SQLite3 (cliente)"]=""
)
declare -A DB_PKG_CANDIDATES=(
    ["MySQL/MariaDB"]="mysql-server mariadb-server mysql-community-server Mariadb-server"
    ["PostgreSQL"]="postgresql postgresql-server"
    ["MongoDB"]="mongodb-org mongodb-org-server mongodb"
    ["Redis"]="redis redis-server"
    ["SQLite3 (cliente)"]="sqlite sqlite3"
)

any_db=0
for db in "${DB_NAMES[@]}"; do
    found_bin=""
    for bin in ${DB_BINS[$db]}; do
        if has_cmd "$bin"; then
            found_bin="$bin"
            break
        fi
    done

    # Tambien contamos como "instalado" si hay una unidad de servicio
    # conocida, aunque el binario no este en el PATH del usuario actual.
    matched_units=""
    if [ -n "${DB_SVC_PATTERN[$db]}" ]; then
        matched_units=$(list_services_matching "${DB_SVC_PATTERN[$db]}")
    fi

    if [ -z "$found_bin" ] && [ -z "$matched_units" ]; then
        continue
    fi
    any_db=1

    version="desconocida"
    case "$db" in
        "MySQL/MariaDB")
            has_cmd mysqld && version=$(mysqld --version 2>/dev/null)
            [ "$version" = "desconocida" ] && has_cmd mariadbd && version=$(mariadbd --version 2>/dev/null)
            [ "$version" = "desconocida" ] && has_cmd mysql && version=$(mysql --version 2>/dev/null)
            ;;
        "PostgreSQL") has_cmd psql && version=$(psql --version 2>/dev/null) ;;
        "MongoDB")    has_cmd mongod && version=$(mongod --version 2>/dev/null | head -1) ;;
        "Redis")      has_cmd redis-server && version=$(redis-server --version 2>/dev/null) ;;
        "SQLite3 (cliente)") has_cmd sqlite3 && version=$(sqlite3 --version 2>/dev/null) ;;
    esac

    running="NO"
    if [ -n "$matched_units" ]; then
        for unit in $matched_units; do
            svc_name="${unit%.service}"
            if service_active "$svc_name"; then
                running="SI (servicio: $unit)"
                break
            fi
        done
    fi
    # Fallback generico por si no hay coincidencia de unidad pero el
    # binario si esta corriendo como proceso.
    if [ "$running" = "NO" ] && [ -n "$found_bin" ] && has_cmd pgrep; then
        pgrep -x "$found_bin" >/dev/null 2>&1 && running="SI (proceso: $found_bin)"
    fi

    listening=""
    for port in ${DB_PORTS[$db]}; do
        if port_listening "$port"; then
            listening="${listening}${listening:+, }puerto $port"
        fi
    done
    [ -n "$listening" ] && listening=" | escuchando en $listening"

    pkg_version=""
    for pkg in ${DB_PKG_CANDIDATES[$db]}; do
        v=$(pkg_info "$pkg" 2>/dev/null)
        if [ -n "$v" ]; then
            pkg_version="$pkg $v"
            break
        fi
    done

    echo -e "${C_OK}[INSTALADO]${C_OFF} $db"
    [ -n "$found_bin" ] && echo "    Binario  : $found_bin"
    echo "    Version  : $version"
    [ -n "$pkg_version" ] && echo "    Paquete  : $pkg_version"
    echo "    Corriendo: $running${listening}"
    echo ""
done

if [ "$any_db" -eq 0 ]; then
    echo -e "${C_WARN}No se detecto ningun motor de base de datos instalado (ni binario ni servicio).${C_OFF}"
fi

echo ""
echo "Puertos de BD comunes actualmente en escucha (todos los procesos):"
port_found=0
if has_cmd ss; then
    for p in 3306 3308 5432 27017 6379 1521 1433; do
        line=$(ss -ltnp 2>/dev/null | grep -E ":${p}[[:space:]]")
        if [ -n "$line" ]; then
            echo "  $line"
            port_found=1
        fi
    done
elif has_cmd netstat; then
    for p in 3306 3308 5432 27017 6379 1521 1433; do
        line=$(netstat -ltnp 2>/dev/null | grep -E ":${p}[[:space:]]")
        if [ -n "$line" ]; then
            echo "  $line"
            port_found=1
        fi
    done
else
    echo "  (ni 'ss' ni 'netstat' disponibles para verificar puertos)"
    port_found=1
fi
[ "$port_found" -eq 0 ] && echo "  (ninguno detectado)"

# Hardening: las BD no deberian escuchar en todas las interfaces (0.0.0.0/::)
# si el servidor no es explicitamente un servidor de base de datos expuesto
# a proposito (en cuyo caso el firewall/VPC deberia limitar el acceso igual).
db_exposed=0
if [ "$any_db" -eq 1 ] && has_cmd ss; then
    exposed_lines=$(ss -ltn 2>/dev/null | grep -E ':(3306|3308|5432|27017|6379)[[:space:]]' | grep -E '(0\.0\.0\.0|\*|\[::\]|:::)')
    [ -n "$exposed_lines" ] && db_exposed=1
fi
if [ "$any_db" -eq 0 ]; then
    record_check "hd_db_exposure" "hardening" "Bases de datos no expuestas en todas las interfaces" "OK" 5 5 ""
elif [ "$db_exposed" -eq 1 ]; then
    record_check "hd_db_exposure" "hardening" "Bases de datos no expuestas en todas las interfaces" "FAIL" 0 5 "Limita el bind address de la base de datos a localhost o a la red interna (bind-address en MySQL/MariaDB, listen_addresses en PostgreSQL, bind en Redis/MongoDB) y restringe el acceso por firewall."
else
    record_check "hd_db_exposure" "hardening" "Bases de datos no expuestas en todas las interfaces" "OK" 5 5 ""
fi

# ------------------------------------------------------------------
# 3) Apache - sitios / vhosts (Debian/Ubuntu usa 'apache2', RHEL/SUSE
#    usan 'httpd'; ambos soportan '-S' para volcar los vhosts efectivos
#    ya resueltos, sin importar la estructura de directorios usada)
# ------------------------------------------------------------------
section "Apache - Sitios (vhosts)"

APACHE_SITES=()
NGINX_SITES=()

APACHE_CTL=""
for c in apache2ctl apachectl httpd; do
    if has_cmd "$c"; then APACHE_CTL="$c"; break; fi
done

if [ -n "$APACHE_CTL" ]; then
    av=$("$APACHE_CTL" -v 2>/dev/null | head -1)
    echo "Apache instalado: ${av:-version desconocida} (control: $APACHE_CTL)"
    echo ""

    apache_running=0
    for svc in apache2 httpd; do
        if service_active "$svc"; then
            echo -e "Estado del servicio ($svc): ${C_OK}activo${C_OFF}"
            apache_running=1
            break
        fi
    done
    [ "$apache_running" -eq 0 ] && echo -e "Estado del servicio: ${C_WARN}inactivo o no gestionado por systemd/init${C_OFF}"
    echo ""

    echo "Resumen de vhosts segun '$APACHE_CTL -S' (fuente principal, portable entre distros):"
    vhost_summary=$("$APACHE_CTL" -S 2>&1)
    if [ -n "$vhost_summary" ]; then
        echo "$vhost_summary" | sed 's/^/  /'
    else
        echo "  (no se pudo obtener; revisa permisos o sintaxis de la config)"
    fi

    # Nombres de sitio (namevhost) para el resumen consolidado de la seccion 5.
    APACHE_SITES=()
    if [ -n "$vhost_summary" ]; then
        while IFS= read -r sn; do
            [ -n "$sn" ] && APACHE_SITES+=("$sn")
        done < <(echo "$vhost_summary" | grep -oE 'namevhost [^ ]+' | awk '{print $2}' | sort -u)
    fi

    # Complemento: si existe el layout tipo Debian (sites-enabled), listamos
    # tambien ServerName/DocumentRoot por archivo para mas detalle.
    apache_root=""
    for d in /etc/apache2 /etc/httpd; do
        [ -d "$d" ] && apache_root="$d" && break
    done

    if [ -n "$apache_root" ] && [ -d "$apache_root/sites-enabled" ]; then
        echo ""
        echo "Detalle adicional (layout Debian: $apache_root/sites-enabled):"
        shopt -s nullglob
        enabled_confs=("$apache_root"/sites-enabled/*.conf)
        shopt -u nullglob
        if [ "${#enabled_confs[@]}" -eq 0 ]; then
            echo "  (sin sitios habilitados)"
        else
            for conf in "${enabled_confs[@]}"; do
                name=$(basename "$conf")
                servernames=$(grep -iE '^\s*ServerName' "$conf" | awk '{print $2}' | sort -u)
                docroot=$(grep -iE '^\s*DocumentRoot' "$conf" | awk '{print $2}' | sort -u | head -1)
                echo "  - $name  ServerName=[${servernames:-N/A}]  DocumentRoot=[${docroot:-N/A}]"
            done
        fi
    elif [ -n "$apache_root" ] && [ -d "$apache_root/conf.d" ]; then
        echo ""
        echo "Detalle adicional (layout RHEL/SUSE: $apache_root/conf.d):"
        shopt -s nullglob
        confs=("$apache_root"/conf.d/*.conf)
        shopt -u nullglob
        if [ "${#confs[@]}" -eq 0 ]; then
            echo "  (sin archivos .conf adicionales)"
        else
            for conf in "${confs[@]}"; do
                grep -qi 'VirtualHost' "$conf" || continue
                name=$(basename "$conf")
                servernames=$(grep -iE '^\s*ServerName' "$conf" | awk '{print $2}' | sort -u)
                docroot=$(grep -iE '^\s*DocumentRoot' "$conf" | awk '{print $2}' | sort -u | head -1)
                echo "  - $name  ServerName=[${servernames:-N/A}]  DocumentRoot=[${docroot:-N/A}]"
            done
        fi
    fi
else
    echo "Apache (apache2/httpd) no esta instalado en este servidor."
fi

# ------------------------------------------------------------------
# 4) Nginx - sitios / server blocks
#    'nginx -T' vuelca la configuracion completa YA RESUELTA (con todos
#    los includes expandidos), por lo que es portable sin importar si
#    la distro usa sites-enabled (Debian) o solo conf.d (RHEL).
# ------------------------------------------------------------------
section "Nginx - Sitios (server blocks)"

if has_cmd nginx; then
    nv=$(nginx -v 2>&1)
    echo "Nginx instalado: $nv"
    echo ""

    nginx_running=0
    if service_active nginx; then
        echo -e "Estado del servicio: ${C_OK}activo${C_OFF}"
        nginx_running=1
    else
        echo -e "Estado del servicio: ${C_WARN}inactivo o no gestionado por systemd/init${C_OFF}"
    fi
    echo ""

    dump=$(nginx -T 2>/dev/null)
    if [ -z "$dump" ]; then
        echo -e "${C_WARN}No se pudo ejecutar 'nginx -T' (revisa permisos; probablemente requiere sudo).${C_OFF}"
        echo "Intentando listar archivos de configuracion conocidos como respaldo:"
        for d in /etc/nginx/sites-enabled /etc/nginx/conf.d; do
            [ -d "$d" ] || continue
            echo "  Directorio: $d"
            ls "$d" 2>/dev/null | sed 's/^/    - /'
        done
    else
        echo "Server blocks encontrados (via 'nginx -T'):"
        echo "$dump" | awk '
            /^# configuration file/ { file=$0; sub(/^# configuration file /, "", file); sub(/:$/, "", file) }
            /server[[:space:]]*\{/ { in_server=1; server_name=""; listen=""; root="" }
            in_server && /server_name/ {
                line=$0; sub(/^[[:space:]]*server_name[[:space:]]*/, "", line); sub(/;.*/, "", line)
                server_name = (server_name == "") ? line : server_name " " line
            }
            in_server && /listen/ {
                line=$0; sub(/^[[:space:]]*listen[[:space:]]*/, "", line); sub(/;.*/, "", line)
                listen = (listen == "") ? line : listen ", " line
            }
            in_server && /[[:space:]]root[[:space:]]/ {
                line=$0; sub(/^[[:space:]]*root[[:space:]]*/, "", line); sub(/;.*/, "", line)
                root = line
            }
            /\}/ && in_server {
                if (server_name != "" || listen != "") {
                    printf "  - archivo=%s\n      server_name=[%s]  listen=[%s]  root=[%s]\n", file, server_name, listen, root
                }
                in_server=0
            }
        '

        # Nombres de sitio (server_name) para el resumen consolidado de la
        # seccion 5. Se descarta el catch-all "_" de nginx (no es un host real).
        while IFS= read -r sn; do
            [ -n "$sn" ] && [ "$sn" != "_" ] && NGINX_SITES+=("$sn")
        done < <(echo "$dump" | grep -E '^[[:space:]]*server_name[[:space:]]' \
            | sed -E 's/^[[:space:]]*server_name[[:space:]]+//; s/;.*//' \
            | tr -s '[:space:]' '\n' | sort -u)
    fi
else
    echo "Nginx no esta instalado en este servidor."
fi

# ------------------------------------------------------------------
# 5) Resumen consolidado de sitios (hostnames) detectados
#    Junta los ServerName de Apache (via 'namevhost' de apache2ctl -S)
#    y los server_name de Nginx (via 'nginx -T') en una sola lista
#    deduplicada, indicando en que servidor(es) web aparece cada uno.
# ------------------------------------------------------------------
section "Resumen de Sitios (Hostnames) Detectados"

ALL_SITES=()
declare -A seen_site=()
for s in "${APACHE_SITES[@]}" "${NGINX_SITES[@]}"; do
    [ -z "$s" ] && continue
    if [ -z "${seen_site[$s]:-}" ]; then
        seen_site[$s]=1
        ALL_SITES+=("$s")
    fi
done

if [ "${#ALL_SITES[@]}" -eq 0 ]; then
    echo "No se detectaron nombres de sitio (ServerName/server_name) en Apache ni Nginx."
    echo "(Puede deberse a que no hay sitios configurados, a que faltan permisos"
    echo " para leer la config, o a que los vhosts usan solo IP/puerto sin ServerName)."
else
    echo "Total de sitios distintos detectados: ${#ALL_SITES[@]}"
    echo ""
    for s in "${ALL_SITES[@]}"; do
        origin=""
        for a in "${APACHE_SITES[@]}"; do [ "$a" = "$s" ] && { origin="${origin}${origin:+, }Apache"; break; }; done
        for n in "${NGINX_SITES[@]}"; do [ "$n" = "$s" ] && { origin="${origin}${origin:+, }Nginx"; break; }; done
        printf "  - %-45s [%s]\n" "$s" "$origin"
    done
fi

# ------------------------------------------------------------------
# 6) PHP - versiones instaladas y en ejecucion
#    Se detectan todos los binarios phpX.Y en PATH (Debian/Ubuntu los
#    nombra asi), ademas del 'php' generico (comun en RHEL/Alpine/Arch),
#    y los servicios FPM via patron de systemd (portable entre distros).
# ------------------------------------------------------------------
section "PHP - Versiones instaladas y en ejecucion"

echo "PHP CLI por defecto:"
if has_cmd php; then
    php -v | head -1 | sed 's/^/  /'
else
    echo "  php (CLI) no esta instalado"
fi

echo ""
echo "Todos los binarios PHP encontrados en PATH (ej. php7.4, php8.1, php8.2):"
found_php_bins=0
IFS=':' read -ra PATH_DIRS <<< "$PATH"
declare -A seen_bins=()
for d in "${PATH_DIRS[@]}"; do
    [ -d "$d" ] || continue
    for f in "$d"/php "$d"/php[0-9]* "$d"/php[0-9].[0-9]*; do
        [ -x "$f" ] || continue
        base=$(basename "$f")
        # ignora phpize, php-config, phpunit, etc.
        case "$base" in
            php|php[0-9].[0-9]|php[0-9][0-9]) ;;
            *) continue ;;
        esac
        [ -n "${seen_bins[$base]:-}" ] && continue
        seen_bins[$base]=1
        ver=$("$f" -v 2>/dev/null | head -1)
        echo "  - $base ($f): $ver"
        found_php_bins=1
    done
done
[ "$found_php_bins" -eq 0 ] && echo "  (ninguno encontrado ademas del 'php' por defecto, si aplica)"

echo ""
echo "Paquetes/servicios PHP-FPM detectados (patron 'php*fpm*' en systemd):"
fpm_units=$(list_services_matching '^php.*fpm.*\.service$')
if [ -n "$fpm_units" ]; then
    for unit in $fpm_units; do
        svc_name="${unit%.service}"
        if service_active "$svc_name"; then
            echo -e "  ${C_OK}[ACTIVO]${C_OFF} $unit"
        else
            echo "  [inactivo] $unit"
        fi
    done
else
    echo "  (ningun servicio con patron php*-fpm encontrado; en RHEL/Alpine el servicio puede llamarse solo 'php-fpm')"
    if service_active php-fpm; then
        echo -e "  ${C_OK}[ACTIVO]${C_OFF} php-fpm"
    fi
fi

echo ""
echo "Modulo de PHP cargado directamente en Apache (mod_php), si aplica:"
if [ -n "${APACHE_CTL:-}" ]; then
    mod_php=$("$APACHE_CTL" -M 2>/dev/null | grep -i php)
    if [ -n "$mod_php" ]; then
        echo "$mod_php" | sed 's/^/  /'
    else
        echo "  (ningun mod_php cargado; probablemente sirve PHP via FPM + proxy/fastcgi)"
    fi
else
    echo "  (Apache no instalado, N/A)"
fi

echo ""
echo "Todas las versiones de PHP-CLI registradas via update-alternatives (si aplica):"
if has_cmd update-alternatives; then
    update-alternatives --list php 2>/dev/null | sed 's/^/  /' || echo "  (php no gestionado por update-alternatives)"
else
    echo "  (update-alternatives no disponible en esta distro; ver lista de binarios de arriba)"
fi

# ------------------------------------------------------------------
# 7) Hardening y Seguridad
#    Firewall, fail2ban, actualizaciones automaticas, hardening de SSH
#    y estado de SELinux/AppArmor (el que aplique segun la distro).
# ------------------------------------------------------------------
section "Hardening y Seguridad"

echo "Firewall:"
fw_found=0
fw_active=0
if has_cmd ufw; then
    fw_found=1
    ufw_status=$(ufw status 2>/dev/null | head -1)
    echo "  ufw       : ${ufw_status:-no se pudo consultar (revisa permisos)}"
    [ "$ufw_status" = "Status: active" ] && fw_active=1
fi
if has_cmd firewall-cmd; then
    fw_found=1
    if service_active firewalld; then
        zone=$(firewall-cmd --get-default-zone 2>/dev/null)
        echo "  firewalld : activo (zona por defecto: ${zone:-desconocida})"
        fw_active=1
    else
        echo "  firewalld : instalado pero inactivo"
    fi
fi
if has_cmd nft; then
    fw_found=1
    nft_rules=$(nft list ruleset 2>/dev/null | grep -cE '^\s*(accept|drop|reject)')
    echo "  nftables  : ${nft_rules:-0} reglas de accept/drop/reject cargadas"
    [ "${nft_rules:-0}" -gt 0 ] && fw_active=1
fi
if has_cmd iptables; then
    fw_found=1
    ipt_rules=$(iptables -L -n 2>/dev/null | grep -cE '^(ACCEPT|DROP|REJECT)')
    echo "  iptables  : ${ipt_rules:-0} reglas activas (tabla filter, requiere permisos para ver todas)"
    [ "${ipt_rules:-0}" -gt 0 ] && fw_active=1
fi
[ "$fw_found" -eq 0 ] && echo -e "  ${C_WARN}No se detecto ufw, firewalld, nftables ni iptables.${C_OFF}"

if [ "$fw_active" -eq 1 ]; then
    record_check "hd_firewall" "hardening" "Firewall activo (ufw/firewalld/nftables/iptables)" "OK" 15 15 ""
else
    record_check "hd_firewall" "hardening" "Firewall activo (ufw/firewalld/nftables/iptables)" "FAIL" 0 15 "Activa y configura un firewall (ufw enable, firewall-cmd, nftables o iptables) que solo permita los puertos estrictamente necesarios."
fi

echo ""
echo "Fail2ban:"
if has_cmd fail2ban-client; then
    if service_active fail2ban; then
        jails=$(fail2ban-client status 2>/dev/null | grep -i "jail list" | sed 's/.*://')
        echo -e "  ${C_OK}[ACTIVO]${C_OFF} jails: ${jails:- (ninguna)}"
        record_check "hd_fail2ban" "hardening" "Fail2ban activo" "OK" 10 10 ""
    else
        echo -e "  ${C_WARN}instalado pero inactivo${C_OFF}"
        record_check "hd_fail2ban" "hardening" "Fail2ban activo" "WARN" 3 10 "Fail2ban esta instalado pero inactivo; habilitalo con 'systemctl enable --now fail2ban'."
    fi
else
    echo -e "  ${C_WARN}no instalado${C_OFF}"
    record_check "hd_fail2ban" "hardening" "Fail2ban activo" "FAIL" 0 10 "Instala fail2ban para mitigar ataques de fuerza bruta contra SSH y otros servicios expuestos."
fi

echo ""
echo "Actualizaciones automaticas de seguridad:"
auto_updates="no detectado"
if service_active unattended-upgrades || systemctl is-enabled --quiet unattended-upgrades.service 2>/dev/null || systemctl is-enabled --quiet apt-daily-upgrade.timer 2>/dev/null; then
    auto_updates="unattended-upgrades / apt-daily-upgrade habilitado (Debian/Ubuntu)"
elif systemctl is-enabled --quiet dnf-automatic.timer 2>/dev/null || systemctl is-enabled --quiet dnf-automatic-install.timer 2>/dev/null; then
    auto_updates="dnf-automatic habilitado (RHEL/Fedora)"
elif service_active yum-cron; then
    auto_updates="yum-cron activo (RHEL/CentOS)"
elif has_cmd apk && [ -f /etc/periodic/daily/apk-upgrade ]; then
    auto_updates="tarea periodica de apk detectada (Alpine)"
fi
echo "  $auto_updates"
if [ "$auto_updates" = "no detectado" ]; then
    record_check "hd_autoupdates" "hardening" "Actualizaciones automaticas de seguridad habilitadas" "FAIL" 0 10 "Habilita actualizaciones automaticas de seguridad (unattended-upgrades en Debian/Ubuntu, dnf-automatic en RHEL/Fedora, yum-cron en CentOS)."
else
    record_check "hd_autoupdates" "hardening" "Actualizaciones automaticas de seguridad habilitadas" "OK" 10 10 ""
fi

echo ""
echo "Hardening de SSH (/etc/ssh/sshd_config):"
SSHD_DUMP=""
if has_cmd sshd && [ "$(id -u)" -eq 0 ]; then
    SSHD_DUMP=$(sshd -T 2>/dev/null)
fi
if [ -z "$SSHD_DUMP" ] && [ ! -f /etc/ssh/sshd_config ]; then
    echo "  (no se encontro sshd ni /etc/ssh/sshd_config; el servidor puede no tener SSH instalado)"
    record_check "hd_ssh_rootlogin" "hardening" "SSH: PermitRootLogin restringido" "INFO" 0 0 ""
    record_check "hd_ssh_passwordauth" "hardening" "SSH: autenticacion por password deshabilitada" "INFO" 0 0 ""
    record_check "hd_ssh_emptypass" "hardening" "SSH: passwords vacios deshabilitados" "INFO" 0 0 ""
    record_check "hd_ssh_x11" "hardening" "SSH: X11Forwarding deshabilitado" "INFO" 0 0 ""
    record_check "hd_ssh_maxauth" "hardening" "SSH: MaxAuthTries limitado (<=4)" "INFO" 0 0 ""
else
    ssh_rootlogin=$(get_ssh_value permitrootlogin PermitRootLogin)
    ssh_passwordauth=$(get_ssh_value passwordauthentication PasswordAuthentication)
    ssh_emptypass=$(get_ssh_value permitemptypasswords PermitEmptyPasswords)
    ssh_x11=$(get_ssh_value x11forwarding X11Forwarding)
    ssh_maxauth=$(get_ssh_value maxauthtries MaxAuthTries)
    ssh_port=$(get_ssh_value port Port)

    echo "  PermitRootLogin       : $ssh_rootlogin"
    echo "  PasswordAuthentication: $ssh_passwordauth"
    echo "  PermitEmptyPasswords  : $ssh_emptypass"
    echo "  X11Forwarding         : $ssh_x11"
    echo "  MaxAuthTries          : $ssh_maxauth"
    echo "  Port                  : $ssh_port"
    [ -z "$SSHD_DUMP" ] && echo "  (leido directamente de sshd_config; ejecuta como root para ver la config efectiva via 'sshd -T')"

    # "no definido" = OpenSSH no lo tiene en sshd_config/sshd -T; desde
    # OpenSSH 7.0 (2015) el default de fabrica ya es 'prohibit-password',
    # por lo que no configurarlo explicitamente NO es un FAIL (a diferencia
    # de dejarlo en 'yes', que si habilita login de root con password).
    if [ "$ssh_rootlogin" = "no" ] || [ "$ssh_rootlogin" = "prohibit-password" ] || [ "$ssh_rootlogin" = "without-password" ] || [[ "$ssh_rootlogin" == "no definido"* ]]; then
        record_check "hd_ssh_rootlogin" "hardening" "SSH: PermitRootLogin restringido" "OK" 15 15 ""
    elif [ "$ssh_rootlogin" = "yes" ]; then
        record_check "hd_ssh_rootlogin" "hardening" "SSH: PermitRootLogin restringido" "FAIL" 0 15 "Configura 'PermitRootLogin no' (o 'prohibit-password') en sshd_config para impedir el login directo como root."
    else
        record_check "hd_ssh_rootlogin" "hardening" "SSH: PermitRootLogin restringido" "WARN" 8 15 "Valor de PermitRootLogin no reconocido ('$ssh_rootlogin'); verifica manualmente la configuracion."
    fi

    if [ "$ssh_passwordauth" = "no" ]; then
        record_check "hd_ssh_passwordauth" "hardening" "SSH: autenticacion por password deshabilitada" "OK" 15 15 ""
    else
        record_check "hd_ssh_passwordauth" "hardening" "SSH: autenticacion por password deshabilitada" "FAIL" 0 15 "Configura 'PasswordAuthentication no' y usa autenticacion por llave publica para reducir el riesgo de fuerza bruta."
    fi

    if [ "$ssh_emptypass" = "no" ] || [[ "$ssh_emptypass" == "no definido"* ]]; then
        record_check "hd_ssh_emptypass" "hardening" "SSH: passwords vacios deshabilitados" "OK" 5 5 ""
    else
        record_check "hd_ssh_emptypass" "hardening" "SSH: passwords vacios deshabilitados" "FAIL" 0 5 "Configura 'PermitEmptyPasswords no' explicitamente en sshd_config."
    fi

    if [ "$ssh_x11" = "no" ]; then
        record_check "hd_ssh_x11" "hardening" "SSH: X11Forwarding deshabilitado" "OK" 5 5 ""
    else
        record_check "hd_ssh_x11" "hardening" "SSH: X11Forwarding deshabilitado" "WARN" 2 5 "Deshabilita 'X11Forwarding' si no es necesario, para reducir la superficie de ataque."
    fi

    if [[ "$ssh_maxauth" =~ ^[0-9]+$ ]] && [ "$ssh_maxauth" -le 4 ]; then
        record_check "hd_ssh_maxauth" "hardening" "SSH: MaxAuthTries limitado (<=4)" "OK" 5 5 ""
    else
        record_check "hd_ssh_maxauth" "hardening" "SSH: MaxAuthTries limitado (<=4)" "WARN" 2 5 "Configura 'MaxAuthTries 4' (o un valor menor) para limitar los intentos de autenticacion por conexion."
    fi
fi

echo ""
echo "SELinux:"
se_available=0
se_enforcing=0
if has_cmd getenforce; then
    se_available=1
    se_state=$(getenforce 2>/dev/null)
    if [ "$se_state" = "Enforcing" ]; then
        echo -e "  Estado: ${C_OK}${se_state}${C_OFF}"
        se_enforcing=1
    else
        echo -e "  Estado: ${C_WARN}${se_state}${C_OFF}"
    fi
    has_cmd sestatus && sestatus 2>/dev/null | sed 's/^/  /'
elif [ -f /sys/fs/selinux/enforce ]; then
    se_available=1
    val=$(cat /sys/fs/selinux/enforce 2>/dev/null)
    if [ "$val" = "1" ]; then
        echo -e "  Estado (via /sys/fs/selinux): ${C_OK}Enforcing${C_OFF}"
        se_enforcing=1
    else
        echo -e "  Estado (via /sys/fs/selinux): ${C_WARN}Permissive${C_OFF}"
    fi
else
    echo "  No instalado / no disponible en este sistema (comun en Debian/Ubuntu, que usan AppArmor)"
fi

echo ""
echo "AppArmor:"
aa_available=0
aa_active=0
if has_cmd aa-status; then
    aa_available=1
    if aa-status --enabled >/dev/null 2>&1; then
        echo -e "  Estado: ${C_OK}activo${C_OFF}"
        aa_active=1
    else
        echo -e "  Estado: ${C_WARN}instalado pero inactivo${C_OFF}"
    fi
    summary=$(aa-status 2>/dev/null | grep -E 'profiles are|processes')
    [ -n "$summary" ] && echo "$summary" | sed 's/^/  /'
elif [ -d /sys/kernel/security/apparmor ]; then
    aa_available=1
    echo "  Modulo del kernel presente (/sys/kernel/security/apparmor) pero 'aa-status' no esta instalado"
else
    echo "  No instalado / no disponible en este sistema (comun en RHEL/Fedora, que usan SELinux)"
fi

if [ "$se_enforcing" -eq 1 ] || [ "$aa_active" -eq 1 ]; then
    record_check "hd_mac" "hardening" "SELinux/AppArmor activo en modo enforcing" "OK" 15 15 ""
elif [ "$se_available" -eq 1 ] || [ "$aa_available" -eq 1 ]; then
    record_check "hd_mac" "hardening" "SELinux/AppArmor activo en modo enforcing" "WARN" 5 15 "SELinux/AppArmor esta presente pero no en modo enforcing/activo. Cambia SELinux a 'Enforcing' (setenforce 1 y SELINUX=enforcing en /etc/selinux/config) o activa el perfil de AppArmor correspondiente."
else
    record_check "hd_mac" "hardening" "SELinux/AppArmor activo en modo enforcing" "FAIL" 0 15 "Instala y activa SELinux (RHEL/Fedora/SUSE) o AppArmor (Debian/Ubuntu) para reforzar el aislamiento de procesos."
fi

# ------------------------------------------------------------------
# 8) Actualizaciones de Paquetes (parches pendientes)
#    Cuenta paquetes desactualizados y, cuando el gestor lo permite,
#    diferencia las actualizaciones de seguridad. Usa 'timeout' en las
#    consultas que dependen de repos remotos (dnf/yum/zypper) para no
#    colgar el script si el servidor no tiene salida a internet.
# ------------------------------------------------------------------
section "Actualizaciones de Paquetes"

PKG_UPDATES_COUNT=""
PKG_SECURITY_COUNT=""
PKG_MGR_USED=""
TIMEOUT_BIN=""
has_cmd timeout && TIMEOUT_BIN="timeout 20"

# pkg_cmd_count <codigos_exito_validos (separados por espacio)> <comando...>
# Ejecuta el comando y solo imprime su salida (para luego contar lineas)
# si termino con uno de los codigos de salida indicados como exitosos.
# Evita el falso "0 pendientes" que resulta de contar lineas de un
# comando que en realidad fallo (sin red, sin permisos, repos rotos,
# timeout, etc.), lo cual antes se reportaba como servidor al dia.
pkg_cmd_count() {
    local ok_codes="$1" code out status
    shift
    out=$("$@" 2>/dev/null)
    status=$?
    for code in $ok_codes; do
        if [ "$status" -eq "$code" ]; then
            printf '%s' "$out"
            return 0
        fi
    done
    return 1
}

if has_cmd apt; then
    PKG_MGR_USED="apt"
    if out=$(pkg_cmd_count "0" apt list --upgradable); then
        PKG_UPDATES_COUNT=$(printf '%s\n' "$out" | grep -vc '^Listing')
    fi
    if out=$(pkg_cmd_count "0" $TIMEOUT_BIN apt-get -s dist-upgrade); then
        PKG_SECURITY_COUNT=$(printf '%s\n' "$out" | grep -ic '^Inst.*-security')
    fi
elif has_cmd dnf; then
    PKG_MGR_USED="dnf"
    if out=$(pkg_cmd_count "0" $TIMEOUT_BIN dnf list updates); then
        PKG_UPDATES_COUNT=$(printf '%s\n' "$out" | tail -n +2 | grep -vc '^$')
    fi
    if out=$(pkg_cmd_count "0" $TIMEOUT_BIN dnf list updates --security); then
        PKG_SECURITY_COUNT=$(printf '%s\n' "$out" | tail -n +2 | grep -vc '^$')
    fi
elif has_cmd yum; then
    PKG_MGR_USED="yum"
    # yum check-update usa codigos de salida especiales: 0 = sin
    # actualizaciones, 100 = hay actualizaciones disponibles, cualquier
    # otro = error real (antes se ignoraba el codigo de salida).
    if out=$(pkg_cmd_count "0 100" $TIMEOUT_BIN yum check-update); then
        PKG_UPDATES_COUNT=$(printf '%s\n' "$out" | awk '/^$/{exit} NR>1{c++} END{print c+0}')
    fi
    if out=$(pkg_cmd_count "0" $TIMEOUT_BIN yum updateinfo list security); then
        PKG_SECURITY_COUNT=$(printf '%s\n' "$out" | grep -c '^[A-Za-z0-9]')
    fi
elif has_cmd zypper; then
    PKG_MGR_USED="zypper"
    if out=$(pkg_cmd_count "0" $TIMEOUT_BIN zypper -q lu); then
        PKG_UPDATES_COUNT=$(printf '%s\n' "$out" | grep -c '^v ')
    fi
    if out=$(pkg_cmd_count "0" $TIMEOUT_BIN zypper -q list-patches --category security); then
        PKG_SECURITY_COUNT=$(printf '%s\n' "$out" | grep -cE '^ *[0-9]')
    fi
elif has_cmd apk; then
    PKG_MGR_USED="apk"
    if out=$(pkg_cmd_count "0" apk version -l "<"); then
        PKG_UPDATES_COUNT=$(printf '%s\n' "$out" | grep -vc '^$')
    fi
    PKG_SECURITY_COUNT=""
elif has_cmd pacman; then
    PKG_MGR_USED="pacman"
    # pacman -Qu sale con codigo 1 (no 0) cuando no hay nada que actualizar.
    if out=$(pkg_cmd_count "0 1" pacman -Qu); then
        PKG_UPDATES_COUNT=$(printf '%s\n' "$out" | grep -vc '^$')
    fi
    PKG_SECURITY_COUNT=""
fi

if [ -z "$PKG_MGR_USED" ]; then
    echo -e "${C_WARN}No se detecto un gestor de paquetes soportado (apt/dnf/yum/zypper/apk/pacman).${C_OFF}"
    record_check "up_pending" "actualizaciones" "Paquetes al dia (sin actualizaciones pendientes)" "INFO" 0 0 ""
    record_check "up_security" "actualizaciones" "Actualizaciones de seguridad al dia" "INFO" 0 0 ""
else
    echo "Gestor de paquetes: $PKG_MGR_USED"
    echo "Paquetes con actualizacion pendiente     : ${PKG_UPDATES_COUNT:-N/D}"
    if [ -n "$PKG_SECURITY_COUNT" ]; then
        echo "Actualizaciones de seguridad pendientes  : $PKG_SECURITY_COUNT"
    else
        echo "Actualizaciones de seguridad pendientes  : no distinguibles con este gestor de paquetes"
    fi

    if [[ "$PKG_UPDATES_COUNT" =~ ^[0-9]+$ ]]; then
        if [ "$PKG_UPDATES_COUNT" -eq 0 ]; then
            record_check "up_pending" "actualizaciones" "Paquetes al dia (sin actualizaciones pendientes)" "OK" 60 60 ""
        elif [ "$PKG_UPDATES_COUNT" -le 5 ]; then
            record_check "up_pending" "actualizaciones" "Paquetes al dia (sin actualizaciones pendientes)" "WARN" 45 60 "Hay $PKG_UPDATES_COUNT paquete(s) con actualizacion disponible; aplica el mantenimiento programado pronto."
        elif [ "$PKG_UPDATES_COUNT" -le 20 ]; then
            record_check "up_pending" "actualizaciones" "Paquetes al dia (sin actualizaciones pendientes)" "WARN" 25 60 "Hay $PKG_UPDATES_COUNT paquetes con actualizacion disponible; programa una ventana de mantenimiento para actualizar."
        else
            record_check "up_pending" "actualizaciones" "Paquetes al dia (sin actualizaciones pendientes)" "FAIL" 0 60 "Hay $PKG_UPDATES_COUNT paquetes desactualizados; el servidor lleva tiempo sin parchear, prioriza la actualizacion."
        fi
    else
        record_check "up_pending" "actualizaciones" "Paquetes al dia (sin actualizaciones pendientes)" "INFO" 0 0 "No se pudo determinar (revisa conectividad a los repositorios o permisos)."
    fi

    if [[ "$PKG_SECURITY_COUNT" =~ ^[0-9]+$ ]]; then
        if [ "$PKG_SECURITY_COUNT" -eq 0 ]; then
            record_check "up_security" "actualizaciones" "Actualizaciones de seguridad al dia" "OK" 40 40 ""
        elif [ "$PKG_SECURITY_COUNT" -le 3 ]; then
            record_check "up_security" "actualizaciones" "Actualizaciones de seguridad al dia" "WARN" 20 40 "Hay $PKG_SECURITY_COUNT actualizacion(es) de seguridad pendientes; aplicalas cuanto antes."
        else
            record_check "up_security" "actualizaciones" "Actualizaciones de seguridad al dia" "FAIL" 0 40 "Hay $PKG_SECURITY_COUNT actualizaciones de seguridad pendientes; parchea de inmediato."
        fi
    else
        record_check "up_security" "actualizaciones" "Actualizaciones de seguridad al dia" "INFO" 0 0 "Este gestor de paquetes no distingue actualizaciones de seguridad especificas."
    fi
fi

# ------------------------------------------------------------------
# 9) Resumen, Puntaje y Recomendaciones
# ------------------------------------------------------------------
section "Resumen, Puntaje y Recomendaciones"

read -r hd_got hd_max <<< "$(category_score hardening)"
read -r up_got up_max <<< "$(category_score actualizaciones)"
hd_pct=0; [ "$hd_max" -gt 0 ] && hd_pct=$(( hd_got * 100 / hd_max ))
up_pct=0; [ "$up_max" -gt 0 ] && up_pct=$(( up_got * 100 / up_max ))

echo "Puntaje de Hardening      : $hd_got/$hd_max pts   $(text_bar "$hd_pct")"
echo "Puntaje de Actualizaciones: $up_got/$up_max pts   $(text_bar "$up_pct")"

echo ""
echo "Detalle de controles evaluados:"
for id in "${CHECK_IDS[@]}"; do
    st="${CHECK_STATUS[$id]}"
    color="$C_INFO"
    case "$st" in
        OK) color="$C_OK" ;;
        WARN) color="$C_WARN" ;;
        FAIL) color="$C_FAIL" ;;
    esac
    printf "  ${color}[%-4s]${C_OFF} %-58s %s/%s pts\n" "$st" "${CHECK_DESC[$id]}" "${CHECK_PTS[$id]}" "${CHECK_MAX[$id]}"
done

echo ""
echo "Recomendaciones (controles en WARN o FAIL):"
any_rec=0
for id in "${CHECK_IDS[@]}"; do
    st="${CHECK_STATUS[$id]}"
    if { [ "$st" = "WARN" ] || [ "$st" = "FAIL" ]; } && [ -n "${CHECK_REC[$id]}" ]; then
        any_rec=1
        echo "  - [${CHECK_CAT[$id]}] ${CHECK_DESC[$id]}: ${CHECK_REC[$id]}"
    fi
done
[ "$any_rec" -eq 0 ] && echo "  (sin recomendaciones pendientes; todos los controles evaluados pasaron)"

# ------------------------------------------------------------------
# Reporte HTML (opcional, via -r archivo.html)
#
# Genera un reporte autocontenido (sin dependencias externas: CSS y SVG
# inline, sin CDNs) con logo, gauge de puntaje general, barras de
# progreso por categoria, tabla de controles y recomendaciones. Sirve
# para comparar visualmente el hardening entre varios servidores.
# ------------------------------------------------------------------
html_escape() {
    sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

generate_html_report() {
    local out_file="$1"
    local hostname_val now_val
    hostname_val=$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)
    now_val=$(date '+%Y-%m-%d %H:%M:%S')

    local overall_got=$(( hd_got + up_got ))
    local overall_max=$(( hd_max + up_max ))
    local overall_pct=0
    [ "$overall_max" -gt 0 ] && overall_pct=$(( overall_got * 100 / overall_max ))

    local radius=80
    local circumference overall_offset
    circumference=$(awk -v r="$radius" 'BEGIN{printf "%.2f", 2*3.14159265*r}')
    overall_offset=$(awk -v c="$circumference" -v p="$overall_pct" 'BEGIN{printf "%.2f", c - (c*p/100)}')

    local gauge_color="#dc2626"
    if [ "$overall_pct" -ge 80 ]; then
        gauge_color="#16a34a"
    elif [ "$overall_pct" -ge 50 ]; then
        gauge_color="#f59e0b"
    fi

    {
        cat <<HTML_HEAD
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reporte de Auditoria - ${hostname_val}</title>
<style>
  :root {
    --ok:#16a34a; --warn:#f59e0b; --fail:#dc2626; --info:#64748b;
    --bg:#0f172a; --panel:#1e293b; --text:#e2e8f0; --muted:#94a3b8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem; background: var(--bg); color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  }
  .report-header {
    display: flex; align-items: center; justify-content: space-between; gap: 1.25rem;
    border-bottom: 2px solid #334155; padding-bottom: 1.25rem; margin-bottom: 1.5rem;
  }
  .report-header h1 { margin: 0; font-size: 1.6rem; }
  .report-header p { margin: 0.25rem 0 0; color: var(--muted); font-size: 0.95rem; }
  .logo-wordmark {
    display: flex; flex-direction: column; align-items: flex-end; line-height: 1;
    flex-shrink: 0; text-align: right;
  }
  .logo-wordmark .logo-main {
    font-size: 1.5rem; font-weight: 800; letter-spacing: 0.06em;
    color: #38bdf8; white-space: nowrap;
  }
  .logo-wordmark .logo-main span { color: #e2e8f0; margin-left: 0.35em; }
  .logo-wordmark .logo-sub {
    margin-top: 0.3rem; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.25em;
    color: var(--muted); text-transform: uppercase;
  }
  .panel {
    background: var(--panel); border-radius: 12px; padding: 1.5rem;
    margin-bottom: 1.5rem; border: 1px solid #334155;
  }
  .scores { display: flex; flex-wrap: wrap; gap: 2rem; align-items: center; }
  .gauge { position: relative; width: 200px; height: 200px; flex-shrink: 0; }
  .gauge svg { transform: rotate(0deg); }
  .gauge-label {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; font-size: 2.2rem; font-weight: 700;
  }
  .gauge-label span { font-size: 0.85rem; font-weight: 400; color: var(--muted); }
  .bars { flex: 1; min-width: 280px; }
  .bar-row { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }
  .bar-row > span:first-child { width: 150px; flex-shrink: 0; }
  .bar-row > span:last-child { width: 90px; text-align: right; flex-shrink: 0; color: var(--muted); }
  .bar-track { flex: 1; height: 14px; background: #334155; border-radius: 7px; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #38bdf8, #16a34a); border-radius: 7px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  .table-wrap { overflow-x: auto; }
  th, td { text-align: left; padding: 0.6rem 0.7rem; border-bottom: 1px solid #334155; }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.75rem; }
  .badge {
    display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
    font-size: 0.75rem; font-weight: 700; color: #0f172a;
  }
  .badge-ok { background: var(--ok); }
  .badge-warn { background: var(--warn); }
  .badge-fail { background: var(--fail); color: #fff; }
  .badge-info { background: var(--info); color: #fff; }
  .rec-section ul { padding-left: 1.1rem; }
  .rec-section li { margin-bottom: 0.6rem; line-height: 1.4; }
  footer { color: var(--muted); font-size: 0.8rem; text-align: center; margin-top: 2rem; }
</style>
</head>
<body>

<header class="report-header">
  <div>
    <h1>Reporte de Auditoria de Servidor</h1>
    <p>Host: <strong>${hostname_val}</strong> &middot; Generado: ${now_val}</p>
  </div>
  <div class="logo-wordmark" aria-label="Solvex Dominicana">
    <div class="logo-main">SOLVEX<span>DOMINICANA</span></div>
    <div class="logo-sub">Auditoria de Infraestructura</div>
  </div>
</header>

<section class="panel scores">
  <div class="gauge">
    <svg width="200" height="200" viewBox="0 0 200 200">
      <circle cx="100" cy="100" r="${radius}" fill="none" stroke="#334155" stroke-width="16"/>
      <circle cx="100" cy="100" r="${radius}" fill="none" stroke="${gauge_color}" stroke-width="16"
        stroke-dasharray="${circumference}" stroke-dashoffset="${overall_offset}"
        stroke-linecap="round" transform="rotate(-90 100 100)"/>
    </svg>
    <div class="gauge-label">${overall_pct}%<span>Score General</span></div>
  </div>
  <div class="bars">
    <div class="bar-row"><span>Hardening</span><div class="bar-track"><div class="bar-fill" style="width:${hd_pct}%"></div></div><span>${hd_got}/${hd_max} pts</span></div>
    <div class="bar-row"><span>Actualizaciones</span><div class="bar-track"><div class="bar-fill" style="width:${up_pct}%"></div></div><span>${up_got}/${up_max} pts</span></div>
  </div>
</section>

<section class="panel">
<h2>Detalle de Controles</h2>
<div class="table-wrap">
<table>
<thead><tr><th>Categoria</th><th>Control</th><th>Estado</th><th>Puntaje</th><th>Recomendacion</th></tr></thead>
<tbody>
HTML_HEAD

        local id st badge_class rec_html desc_html
        for id in "${CHECK_IDS[@]}"; do
            st="${CHECK_STATUS[$id]}"
            badge_class="badge-info"
            case "$st" in
                OK) badge_class="badge-ok" ;;
                WARN) badge_class="badge-warn" ;;
                FAIL) badge_class="badge-fail" ;;
            esac
            rec_html=$(printf '%s' "${CHECK_REC[$id]}" | html_escape)
            desc_html=$(printf '%s' "${CHECK_DESC[$id]}" | html_escape)
            printf '<tr><td>%s</td><td>%s</td><td><span class="badge %s">%s</span></td><td>%s/%s</td><td>%s</td></tr>\n' \
                "${CHECK_CAT[$id]}" "$desc_html" "$badge_class" "$st" "${CHECK_PTS[$id]}" "${CHECK_MAX[$id]}" "${rec_html:--}"
        done

        cat <<HTML_MID
</tbody>
</table>
</div>
</section>

<section class="panel">
<h2>Sitios (Hostnames) Detectados</h2>
<div class="table-wrap">
<table>
<thead><tr><th>Sitio</th><th>Origen</th></tr></thead>
<tbody>
HTML_MID

        local site site_html origin_html
        if [ "${#ALL_SITES[@]}" -eq 0 ]; then
            echo "<tr><td colspan=\"2\">No se detectaron sitios (ServerName/server_name) en Apache ni Nginx.</td></tr>"
        else
            for site in "${ALL_SITES[@]}"; do
                origin_html=""
                for a in "${APACHE_SITES[@]}"; do [ "$a" = "$site" ] && { origin_html="${origin_html}${origin_html:+, }Apache"; break; }; done
                for nsite in "${NGINX_SITES[@]}"; do [ "$nsite" = "$site" ] && { origin_html="${origin_html}${origin_html:+, }Nginx"; break; }; done
                site_html=$(printf '%s' "$site" | html_escape)
                printf '<tr><td>%s</td><td>%s</td></tr>\n' "$site_html" "$origin_html"
            done
        fi

        cat <<HTML_MID2
</tbody>
</table>
</div>
</section>

<section class="panel rec-section">
<h2>Recomendaciones Prioritarias</h2>
<ul>
HTML_MID2

        local rec_found=0
        for id in "${CHECK_IDS[@]}"; do
            st="${CHECK_STATUS[$id]}"
            if { [ "$st" = "WARN" ] || [ "$st" = "FAIL" ]; } && [ -n "${CHECK_REC[$id]}" ]; then
                rec_found=1
                rec_html=$(printf '%s' "${CHECK_REC[$id]}" | html_escape)
                desc_html=$(printf '%s' "${CHECK_DESC[$id]}" | html_escape)
                badge_class="badge-warn"; [ "$st" = "FAIL" ] && badge_class="badge-fail"
                printf '<li><span class="badge %s">%s</span> <strong>%s:</strong> %s</li>\n' "$badge_class" "$st" "$desc_html" "$rec_html"
            fi
        done
        [ "$rec_found" -eq 0 ] && echo "<li>Sin recomendaciones pendientes; todos los controles evaluados pasaron.</li>"

        cat <<HTML_FOOT
</ul>
</section>

<footer>Generado por server-audit.sh &middot; ${now_val}</footer>
</body>
</html>
HTML_FOOT
    } > "$out_file"

    echo ""
    echo "Reporte HTML generado en: $out_file"
}

# ------------------------------------------------------------------
# Reporte Markdown (opcional, via -m archivo.md)
#
# Mismo contenido que el reporte HTML (puntajes, tabla de controles,
# recomendaciones) pero en Markdown puro, util para pegar en un wiki,
# un README o un Pull Request.
# ------------------------------------------------------------------
generate_markdown_report() {
    local out_file="$1"
    local hostname_val now_val
    hostname_val=$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)
    now_val=$(date '+%Y-%m-%d %H:%M:%S')

    local overall_got=$(( hd_got + up_got ))
    local overall_max=$(( hd_max + up_max ))
    local overall_pct=0
    [ "$overall_max" -gt 0 ] && overall_pct=$(( overall_got * 100 / overall_max ))

    {
        echo "# Reporte de Auditoria de Servidor"
        echo
        echo "**SOLVEX DOMINICANA** &middot; Auditoria de Infraestructura"
        echo
        echo "- **Host:** ${hostname_val}"
        echo "- **Generado:** ${now_val}"
        echo "- **Distro:** ${PRETTY_NAME:-desconocida}"
        echo
        echo "## Puntaje General: ${overall_pct}% (${overall_got}/${overall_max} pts)"
        echo
        echo "| Categoria | Puntaje | % |"
        echo "|---|---|---|"
        echo "| Hardening | ${hd_got}/${hd_max} pts | ${hd_pct}% |"
        echo "| Actualizaciones | ${up_got}/${up_max} pts | ${up_pct}% |"
        echo
        echo "## Detalle de Controles"
        echo
        echo "| Categoria | Control | Estado | Puntaje | Recomendacion |"
        echo "|---|---|---|---|---|"
        local id st
        for id in "${CHECK_IDS[@]}"; do
            st="${CHECK_STATUS[$id]}"
            printf '| %s | %s | %s | %s/%s | %s |\n' \
                "${CHECK_CAT[$id]}" "${CHECK_DESC[$id]}" "$st" "${CHECK_PTS[$id]}" "${CHECK_MAX[$id]}" "${CHECK_REC[$id]:--}"
        done
        echo
        echo "## Sitios (Hostnames) Detectados"
        echo
        if [ "${#ALL_SITES[@]}" -eq 0 ]; then
            echo "No se detectaron sitios (ServerName/server_name) en Apache ni Nginx."
        else
            echo "| Sitio | Origen |"
            echo "|---|---|"
            local site origin_md
            for site in "${ALL_SITES[@]}"; do
                origin_md=""
                for a in "${APACHE_SITES[@]}"; do [ "$a" = "$site" ] && { origin_md="${origin_md}${origin_md:+, }Apache"; break; }; done
                for nsite in "${NGINX_SITES[@]}"; do [ "$nsite" = "$site" ] && { origin_md="${origin_md}${origin_md:+, }Nginx"; break; }; done
                printf '| %s | %s |\n' "$site" "$origin_md"
            done
        fi
        echo
        echo "## Recomendaciones Prioritarias"
        echo
        local rec_found=0
        for id in "${CHECK_IDS[@]}"; do
            st="${CHECK_STATUS[$id]}"
            if { [ "$st" = "WARN" ] || [ "$st" = "FAIL" ]; } && [ -n "${CHECK_REC[$id]}" ]; then
                rec_found=1
                echo "- **[${st}] ${CHECK_DESC[$id]}:** ${CHECK_REC[$id]}"
            fi
        done
        [ "$rec_found" -eq 0 ] && echo "- Sin recomendaciones pendientes; todos los controles evaluados pasaron."
        echo
        echo "---"
        echo "_Generado por server-audit.sh &middot; ${now_val}_"
    } > "$out_file"

    echo ""
    echo "Reporte Markdown generado en: $out_file"
}

# ------------------------------------------------------------------
# Exportacion JSON (opcional, via -j archivo.json)
#
# Formato plano pensado para ser consumido por compare-audits.sh y asi
# comparar el hardening/actualizaciones de varios servidores en un solo
# reporte consolidado.
# ------------------------------------------------------------------
json_escape() {
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' '
}

generate_json_report() {
    local out_file="$1"
    local hostname_val now_val
    hostname_val=$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)
    now_val=$(date '+%Y-%m-%d %H:%M:%S')

    local overall_got=$(( hd_got + up_got ))
    local overall_max=$(( hd_max + up_max ))
    local overall_pct=0
    [ "$overall_max" -gt 0 ] && overall_pct=$(( overall_got * 100 / overall_max ))

    {
        printf '{\n'
        printf '  "hostname": "%s",\n' "$(printf '%s' "$hostname_val" | json_escape)"
        printf '  "generated_at": "%s",\n' "$(printf '%s' "$now_val" | json_escape)"
        printf '  "os": "%s",\n' "$(printf '%s' "${PRETTY_NAME:-desconocida}" | json_escape)"
        printf '  "overall_score": %s,\n' "$overall_pct"
        printf '  "overall_points": {"got": %s, "max": %s},\n' "$overall_got" "$overall_max"
        printf '  "hardening": {"got": %s, "max": %s, "pct": %s},\n' "$hd_got" "$hd_max" "$hd_pct"
        printf '  "updates": {"got": %s, "max": %s, "pct": %s},\n' "$up_got" "$up_max" "$up_pct"
        printf '  "checks": [\n'
        local id st n total_ids
        total_ids=${#CHECK_IDS[@]}
        n=0
        for id in "${CHECK_IDS[@]}"; do
            n=$((n+1))
            st="${CHECK_STATUS[$id]}"
            printf '    {"id": "%s", "category": "%s", "description": "%s", "status": "%s", "points": %s, "max": %s, "recommendation": "%s"}%s\n' \
                "$(printf '%s' "$id" | json_escape)" \
                "$(printf '%s' "${CHECK_CAT[$id]}" | json_escape)" \
                "$(printf '%s' "${CHECK_DESC[$id]}" | json_escape)" \
                "$st" "${CHECK_PTS[$id]}" "${CHECK_MAX[$id]}" \
                "$(printf '%s' "${CHECK_REC[$id]}" | json_escape)" \
                "$([ "$n" -lt "$total_ids" ] && echo ',' || echo '')"
        done
        printf '  ]\n'
        printf '  ,"sites": [\n'
        local site n_sites total_sites origin_json
        total_sites=${#ALL_SITES[@]}
        n_sites=0
        for site in "${ALL_SITES[@]}"; do
            n_sites=$((n_sites+1))
            origin_json=""
            for a in "${APACHE_SITES[@]}"; do [ "$a" = "$site" ] && { origin_json="${origin_json}${origin_json:+, }apache"; break; }; done
            for nsite in "${NGINX_SITES[@]}"; do [ "$nsite" = "$site" ] && { origin_json="${origin_json}${origin_json:+, }nginx"; break; }; done
            printf '    {"name": "%s", "source": "%s"}%s\n' \
                "$(printf '%s' "$site" | json_escape)" \
                "$(printf '%s' "$origin_json" | json_escape)" \
                "$([ "$n_sites" -lt "$total_sites" ] && echo ',' || echo '')"
        done
        printf '  ]\n'
        printf '}\n'
    } > "$out_file"

    echo ""
    echo "Reporte JSON generado en: $out_file"
}

if [ -n "$HTML_REPORT" ]; then
    generate_html_report "$HTML_REPORT"
fi

if [ -n "$MD_REPORT" ]; then
    generate_markdown_report "$MD_REPORT"
fi

if [ -n "$JSON_REPORT" ]; then
    generate_json_report "$JSON_REPORT"
fi

# ------------------------------------------------------------------
# Fin
# ------------------------------------------------------------------
echo ""
echo "=================================================================="
echo " Fin del reporte - $(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)"
echo "=================================================================="
if [ -n "$OUTPUT_FILE" ]; then
    echo "Reporte guardado en: $OUTPUT_FILE"
fi
