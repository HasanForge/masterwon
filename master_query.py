"""
Query WON2 / NeuWON GoldSrc master servers for Half-Life 1 server lists,
then verify which of those servers are actually alive (legacy "details"
query) and pull back their name / map / player count.

Protocol notes
---------------
Master query (client -> master), Valve's legacy "Master Server Query
Protocol" that GoldSrc still uses:

    0x31  <region byte 0xFF = all>  "<last ip:port>\\0"  "<filter>\\0"

The first request seeds with "0.0.0.0:0". The master replies with a
packet starting "\\xff\\xff\\xff\\xff" + "f\\n", followed by 6-byte
blocks (4 IP octets + 2-byte port, big-endian/network order), ending
with a 0.0.0.0:0 terminator. To get more than one packet's worth of
servers you re-query using the last IP:port you received as the new
seed, until you get the terminator or nothing new.

Per-server liveness check uses the legacy WON/WON(2) "details" query -
"\\xff\\xff\\xff\\xff" + "details\\0" - not the Steam-era "TSource Engine
Query" string. That distinction matters here: WON2 keeps old, pre-Steam
HLDS builds alive, and those only understand the original command set
(details/players/rules), not the newer challenge-based A2S_INFO that
Valve introduced with Steam integration. A server can reply with the
classic 'm'-format payload, or occasionally split across several UDP
packets (flagged with an FE FF FF FF header) if the payload is large,
which gets reassembled before parsing. The 'I'-format (newer/bridged
servers) is parsed too, in case one of those turns up on WON2 anyway.
"""

import logging
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("master_query")

# --- Known WON2 / NeuWON master servers ------------------------------------
# The original WON masters (half-life.*.won.net) have been dead since WON
# shut down in 2004. These are the community-run replacements. Edit this
# list if the network's addresses change.
MASTER_SERVERS = [
    ("master.won2.steamlessproject.nl", 27010),
    ("master2.won2.steamlessproject.nl", 27010),
    ("master3.won2.steamlessproject.nl", 27010),
    ("master4.won2.steamlessproject.nl", 27010),
    ("209.173.38.9", 27010),          # WON2 fallback IP
    ("hlmaster1.neuwon.net", 27010),  # NeuWON - alternative revival network
    ("hlmaster2.neuwon.net", 27010),
    ("hlmaster3.neuwon.net", 27010),
]

REGION_ALL = 0xFF
START_SEED = b"0.0.0.0:0"


MASTER_HEADER = b"\xff\xff\xff\xff\x66\x0a"  # "\xff\xff\xff\xff" + "f\n"


def query_master(host, port, filter_str="\\gamedir\\*", timeout=3.0, max_packets=20):
    """
    Query a single GoldSrc-style master server. Returns a set of
    (ip, port) tuples it handed back (possibly empty if it's down,
    unresolvable, or has nothing to offer).
    """
    found = set()
    seed = START_SEED

    try:
        ip = socket.gethostbyname(host)
    except socket.gaierror as exc:
        logger.warning("could not resolve master %s: %s", host, exc)
        return found

    addr = (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    try:
        for _ in range(max_packets):
            query = (
                bytes([0x31, REGION_ALL])
                + seed + b"\x00"
                + filter_str.encode("latin-1") + b"\x00"
            )
            sock.sendto(query, addr)

            try:
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                break

            if len(data) < len(MASTER_HEADER) or not data.startswith(MASTER_HEADER):
                break

            # WON2's master puts a variable amount of zero-byte padding
            # between the header and the first 6-byte record - skip only
            # that contiguous run, then every record is a strict 6-byte
            # stride (4 IP octets + 2-byte big-endian port). Reading
            # records starting right at byte 6 with no padding skip is
            # what causes the "right IP, wrong port" corruption: every
            # record after the padding ends up misaligned.
            pos = len(MASTER_HEADER)
            while pos < len(data) and data[pos] == 0:
                pos += 1

            new_this_packet = 0
            last_ip_port = None

            while pos + 6 <= len(data):
                ip_bytes = data[pos:pos + 4]
                port_bytes = data[pos + 4:pos + 6]

                if ip_bytes == b"\x00\x00\x00\x00" and port_bytes == b"\x00\x00":
                    break  # terminator

                port_num = struct.unpack("!H", port_bytes)[0]

                # Validate before trusting the read - a 0.x.x.x or
                # multicast-range (>=224) first octet, or a zero port,
                # means we've run off the end of real data rather than
                # found a genuine server.
                if ip_bytes[0] == 0 or ip_bytes[0] >= 224 or port_num == 0:
                    break

                ip_str = ".".join(str(b) for b in ip_bytes)
                last_ip_port = (ip_str, port_num)
                if (ip_str, port_num) not in found:
                    new_this_packet += 1
                found.add((ip_str, port_num))
                pos += 6

            if last_ip_port is None or new_this_packet == 0:
                break

            seed = f"{last_ip_port[0]}:{last_ip_port[1]}".encode("latin-1")
    finally:
        sock.close()

    return found


def query_all_masters(filter_str="\\gamedir\\*", timeout=3.0, workers=8):
    """Query every configured master server in parallel, merge the results."""
    servers = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(query_master, host, port, filter_str, timeout): (host, port)
            for host, port in MASTER_SERVERS
        }
        for fut in as_completed(futures):
            host, port = futures[fut]
            try:
                result = fut.result()
                if result:
                    logger.info("master %s:%s -> %d servers", host, port, len(result))
                servers |= result
            except Exception as exc:
                logger.warning("master %s:%s failed: %s", host, port, exc)
    return servers


# --- Legacy WON/WON2 "details" query: is this game server actually alive? -

LEGACY_DETAILS_REQUEST = b"\xff\xff\xff\xffdetails\x00"
SPLIT_HEADER = b"\xfe\xff\xff\xff"


def _read_cstring(data, offset):
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("latin-1", errors="replace"), end + 1


def _reassemble_split(sock, first_packet):
    """
    A split GoldSrc response starts with FE FF FF FF, a 4-byte request
    id, then a split-info byte whose high nibble is this packet's index
    and low nibble is the total packet count. Keep receiving until every
    index 0..total-1 has arrived, then concatenate the payloads in order.
    """
    parts = {}
    data = first_packet
    total = None

    while True:
        if len(data) < 9:
            raise ValueError("split packet shorter than 9 bytes")
        split_byte = data[8]
        packet_number = (split_byte & 0xF0) >> 4
        total = split_byte & 0x0F
        if total <= 0:
            raise ValueError("invalid split count")
        parts[packet_number] = data[9:]
        if len(parts) >= total:
            break
        data, _ = sock.recvfrom(65535)

    return b"".join(parts[i] for i in range(total) if i in parts)


def query_server_info(ip, port, timeout=1.5):
    """
    Send the legacy WON/WON2 "details" query to one game server. Returns
    a dict if it answers (meaning it's genuinely alive, not a stale
    master-list entry), or None if it's unreachable/dead.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    start = time.monotonic()
    try:
        sock.sendto(LEGACY_DETAILS_REQUEST, (ip, port))
        data, _ = sock.recvfrom(65535)
        if data[:4] == SPLIT_HEADER:
            data = _reassemble_split(sock, data)
    except socket.timeout:
        logger.debug("no reply from %s:%s (timed out after %.1fs)", ip, port, timeout)
        return None
    except (OSError, ValueError, IndexError) as exc:
        logger.debug("query to %s:%s failed: %s: %s", ip, port, type(exc).__name__, exc)
        return None
    finally:
        sock.close()

    ping_ms = round((time.monotonic() - start) * 1000)

    if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff":
        return None

    kind = data[4:5]
    offset = 5
    name = map_name = game = folder = ""
    players = max_players = 0
    secure = False

    try:
        if kind == b"m":
            # Legacy GoldSrc/WON(2) "details" response
            _, offset = _read_cstring(data, offset)          # server address
            name, offset = _read_cstring(data, offset)
            map_name, offset = _read_cstring(data, offset)
            folder, offset = _read_cstring(data, offset)
            game, offset = _read_cstring(data, offset)
            players = data[offset]; offset += 1
            max_players = data[offset]; offset += 1
            offset += 1  # protocol
            offset += 1  # server type
            offset += 1  # environment
            offset += 1  # password flag
            mod = data[offset]; offset += 1
            if mod:
                _, offset = _read_cstring(data, offset)      # mod website
                _, offset = _read_cstring(data, offset)      # mod download
                offset += 1                                   # null separator
                offset += 4                                   # mod version (long)
                offset += 4                                   # mod size (long)
                offset += 1                                   # mod type
                offset += 1                                   # mod dll
            if offset < len(data):
                secure = bool(data[offset])
            # a trailing bot-count byte can follow; not used here
        elif kind == b"I":
            # Newer protocol-48 style response
            offset += 1  # protocol version
            name, offset = _read_cstring(data, offset)
            map_name, offset = _read_cstring(data, offset)
            folder, offset = _read_cstring(data, offset)
            game, offset = _read_cstring(data, offset)
            offset += 2  # app id (short)
            players = data[offset]; offset += 1
            max_players = data[offset]; offset += 1
            offset += 1  # bots
            offset += 1  # server type
            offset += 1  # environment
            offset += 1  # visibility
            if offset < len(data):
                secure = bool(data[offset])
        else:
            return None
    except (IndexError, ValueError):
        return None

    return {
        "ip": ip,
        "port": port,
        "name": name or "(unnamed server)",
        "map": map_name,
        "game": game or folder,
        "players": players,
        "max_players": max_players,
        "secure": secure,
        "ping_ms": ping_ms,
    }


def collect_working_servers(filter_str="\\gamedir\\*", master_timeout=3.0, info_timeout=2.5, workers=30):
    """
    Ask every master for its server list, then probe every candidate
    with the legacy "details" query in parallel and keep only the ones
    that actually answer. Returns (working_servers_list, total_candidate_count).
    """
    candidates = query_all_masters(filter_str=filter_str, timeout=master_timeout)
    working = []

    if not candidates:
        return working, 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(query_server_info, ip, port, info_timeout)
            for ip, port in candidates
        ]
        for fut in as_completed(futures):
            info = fut.result()
            if info:
                working.append(info)

    working.sort(key=lambda s: s["ping_ms"])
    logger.info(
        "probed %d candidates, %d answered (filter=%r, timeout=%.1fs)",
        len(candidates), len(working), filter_str, info_timeout,
    )
    return working, len(candidates)


def probe_candidates(candidates, info_timeout=2.5, workers=30):
    """
    Debug helper: ping every candidate and report EVERY result (alive
    or dead), not just the ones that answered. Used by /api/debug so
    you can see exactly which servers are being tried and what happens
    to each one, instead of just a final count.
    """
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(query_server_info, ip, port, info_timeout): (ip, port)
            for ip, port in candidates
        }
        for fut in as_completed(futures):
            ip, port = futures[fut]
            info = fut.result()
            results.append({"ip": ip, "port": port, "alive": info is not None, "info": info})
    results.sort(key=lambda r: (not r["alive"], r["ip"], r["port"]))
    return results
