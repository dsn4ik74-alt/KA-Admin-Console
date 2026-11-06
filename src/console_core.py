"""Discovery, file parsing and read-only RAC requests for 1C Console."""
from __future__ import annotations

import concurrent.futures
import ctypes
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from local_ras import SearchCancelled, decode_output, connect_with_fallback
from ras_tools import managed_tools

RAC_TIMEOUT = 8
DEFAULT_RAS_PORT = 1545
VERSION_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
LST_RE = re.compile(
    r'\{\s*"tcp"\s*,\s*"((?:[^"]|"")*)"\s*,\s*(\d+)\s*,\s*"((?:[^"]|"")*)"\s*\}',
    re.IGNORECASE,
)


def decode_text(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1251", "cp866"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def read_lst(path):
    servers = {}
    seen = set()
    for match in LST_RE.finditer(decode_text(Path(path).read_bytes())):
        server, port, description = match.groups()
        server = server.replace('""', '"').strip()
        description = description.replace('""', '"').strip()
        if not server or not 0 < int(port) <= 65535:
            continue
        port = str(int(port))
        key = (server.casefold(), port, description)
        if key in seen:
            continue
        seen.add(key)
        version = VERSION_RE.search(description)
        servers.setdefault(server, []).append({
            "port": port, "version": version.group() if version else "",
            "descr": description,
        })
    return servers


def path_key(path):
    return os.path.normcase(os.path.realpath(path))


def unique_paths(paths):
    seen = set()
    for path in paths:
        path = Path(path)
        key = path_key(path)
        if key not in seen:
            seen.add(key)
            yield path


def known_folder(csidl):
    """Honor Windows folder redirection, including paths outside USERPROFILE."""
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer) == 0:
            return Path(buffer.value)
    return None


def profile_appdata(profile):
    return [profile / "AppData" / "Local", profile / "AppData" / "Roaming",
            profile / "Local Settings" / "Application Data", profile / "Application Data"]


def local_roots():
    """Return 1C directories of the account running this process only."""
    home = Path.home()
    current = profile_appdata(home)
    for variable in ("LOCALAPPDATA", "APPDATA"):
        if os.environ.get(variable):
            current.insert(0, Path(os.environ[variable]))
    for csidl in (26, 28):  # Roaming AppData, Local AppData
        folder = known_folder(csidl)
        if folder:
            current.insert(0, folder)
    return [(path / "1C", True) for path in unique_paths(current)]


@dataclass
class LstDiscovery:
    paths: list[str] = field(default_factory=list)
    current_paths: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def discover_local_lst(roots=None):
    """Scan known directories, never recursively traverse profile caches.

    roots can be supplied explicitly for diagnostics and isolated tests.
    location.cfg chains are bounded and cycles are ignored.
    """
    report = LstDiscovery()
    visited = set()
    found = set()

    def inspect(directory, current, depth=0):
        key = path_key(directory)
        if (key, current) in visited or depth >= 32:
            return
        visited.add((key, current))
        candidate = directory / "appsrvrs.lst"
        report.checked.append(str(candidate))
        try:
            # Open instead of exists(): access denied must not mean 'not found'.
            with candidate.open("rb"):
                pass
        except FileNotFoundError:
            pass
        except OSError as exc:
            report.errors.append(f"{candidate}: {exc.strerror or type(exc).__name__}")
        else:
            if key not in found:
                found.add(key)
                report.paths.append(str(candidate))
            if current and str(candidate) not in report.current_paths:
                report.current_paths.append(str(candidate))
        config = directory / "location.cfg"
        try:
            content = decode_text(config.read_bytes())
        except FileNotFoundError:
            return
        except OSError as exc:
            report.errors.append(f"{config}: {exc.strerror or type(exc).__name__}")
            return
        match = re.search(r"^\s*location\s*=\s*(.*?)\s*$", content, re.MULTILINE | re.IGNORECASE)
        # Older installations can also contain a plain directory path.
        value = match.group(1) if match else content.strip()
        value = value.strip().strip('"')
        if not value or "\n" in value or "=" in value:
            return
        target = Path(os.path.expandvars(value))
        if not target.is_absolute():
            target = directory / target
        inspect(target, current, depth + 1)

    for root, current in (local_roots() if roots is None else roots):
        if not current:
            continue
        root = Path(root)
        directories = [root / name for name in ("1cv8", "1cv82", "1cv83")]
        try:
            directories.extend(p for p in root.iterdir()
                               if p.name.casefold().startswith("1cv8") and p.is_dir())
        except FileNotFoundError:
            pass
        except OSError as exc:
            report.errors.append(f"{root}: {exc.strerror or type(exc).__name__}")
        for directory in unique_paths(directories):
            inspect(directory, current)
    return report


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def platform_roots():
    roots = [Path(os.environ.get(env, default)) / "1cv8" for env, default in (
        ("ProgramW6432", r"C:\Program Files"), ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
    for root, _ in local_roots():
        roots.extend([root, root / "1cv8"])
    # Per-user platform installs use Programs, not the appsrvrs.lst directory.
    local = Path(os.environ.get('LOCALAPPDATA') or known_folder(28) or Path.home() / 'AppData' / 'Local')
    roots.extend(local / 'Programs' / name for name in ('1cv8', '1cv8_x86', '1cv8_x64'))
    roots.extend(registered_platform_roots())
    roots.extend(Path(f"{drive}:/{name}") for drive in "CDE" for name in ("1cv8", "1C"))
    return list(unique_paths(roots))


def registered_platform_roots():
    """Read registered 1C locations, including installs on a custom drive."""
    if os.name != 'nt':
        return []
    import winreg
    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                    0, winreg.KEY_READ | view) as uninstall:
                    for index in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as entry:
                                name = str(winreg.QueryValueEx(entry, 'DisplayName')[0]).casefold()
                                if not re.search(r'1[cс].*(?:enterprise|предприят)', name):
                                    continue
                                location = winreg.QueryValueEx(entry, 'InstallLocation')[0]
                                if not isinstance(location, str) or not location.strip():
                                    continue
                                directory = Path(os.path.expandvars(location.strip().strip('"')))
                                if not directory.is_absolute():
                                    continue
                                if directory.name.casefold() == 'bin':
                                    directory = directory.parent
                                roots.append(directory.parent if VERSION_RE.fullmatch(directory.name) else directory)
                        except OSError:
                            continue
            except OSError:
                continue
    return roots


def find_all_rac():
    versions = {}
    for root in platform_roots():
        try:
            for directory in root.iterdir():
                if VERSION_RE.fullmatch(directory.name):
                    rac = directory / "bin" / "rac.exe"
                    if rac.is_file():
                        previous = versions.get(directory.name)
                        if (previous is None or (not Path(previous).with_name("ras.exe").is_file()
                                                 and rac.with_name("ras.exe").is_file())):
                            versions[directory.name] = str(rac)
        except OSError:
            continue
    return versions


def find_rac_best(versions):
    return versions[max(versions, key=version_key)] if versions else None


def parse_rac(text):
    blocks, current = [], {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = {}
        elif ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('""', '"')
            current[key.strip()] = value
    if current:
        blocks.append(current)
    return blocks


def run_rac(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=RAC_TIMEOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"RAS не ответил за {RAC_TIMEOUT} с") from None
    except OSError as exc:
        raise RuntimeError(f"Не удалось запустить rac.exe: {exc.strerror}") from None

    if result.returncode:
        detail = (decode_output(result.stderr) or decode_output(result.stdout)).strip()
        # Do not propagate credentials even if a utility echoes its arguments.
        for argument in cmd:
            if argument.startswith("--cluster-pwd=") and argument.partition("=")[2]:
                detail = detail.replace(argument.partition("=")[2], "***")
        raise RuntimeError(detail[:600] or f"rac.exe: код ошибки {result.returncode}")
    return decode_output(result.stdout)


def collect_bases(items, versions, user, password, notify=lambda *args: None,
                  stop=None, runner=run_rac, ras_ports=None, local_ras=None):
    """Two bounded request batches; this function never reads or writes Tk widgets."""
    if local_ras is not None:
        return collect_bases_local(items, versions, user, password, local_ras, notify, stop, runner)
    best = find_rac_best(versions)
    if not best:
        raise RuntimeError("rac.exe не найден. Установите компоненты администрирования 1С.")
    targets = {}
    for item in items:
        targets.setdefault((item["server"].casefold(), item["port"]), item)
    errors, clusters, found = [], [], []
    ras_ports = ras_ports or {}

    def cancelled():
        return stop is not None and stop.is_set()

    def batch(jobs, stage, workers):
        if not jobs or cancelled():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
            pending = {executor.submit(runner, cmd): info for info, cmd in jobs}
            for done, future in enumerate(concurrent.futures.as_completed(pending), 1):
                if cancelled():
                    for waiting in pending:
                        waiting.cancel()
                    return
                info = pending[future]
                notify(stage, done, len(jobs))
                try:
                    output = future.result()
                except Exception as exc:
                    errors.append(f"{info['endpoint']}: {exc}")
                    continue
                yield info, parse_rac(output)

    jobs = []
    for item in targets.values():
        try:
            port = int(ras_ports.get(f"{item['server']}:{item['port']}", DEFAULT_RAS_PORT))
            if not 0 < port <= 65535:
                raise ValueError
        except (ValueError, TypeError):
            errors.append(f"{item['server']}: некорректный порт RAS")
            continue
        host = item["server"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        info = dict(item, endpoint=f"{host}:{port}", rac=versions.get(item.get("version"), best))
        jobs.append((info, [info["rac"], "cluster", "list", info["endpoint"]]))
    for info, blocks in batch(jobs, "Опрос серверов", 10):
        for cluster in blocks:
            if cluster.get("cluster"):
                clusters.append(dict(info, cluster_id=cluster["cluster"], cluster_name=cluster.get("name", ""),
                                     cluster_host=cluster.get('host', ''), cluster_port=cluster.get('port', '')))
    jobs = []
    # A cluster with no administrators accepts a request without credentials.
    # Preserve an explicitly empty password when a username is provided.
    credentials = [f"--cluster-user={user}", f"--cluster-pwd={password}"] if user or password else []
    for info in clusters:
        cmd = [info["rac"], "infobase", "summary", "list", f"--cluster={info['cluster_id']}",
               *credentials, info["endpoint"]]
        jobs.append((info, cmd))
    for info, blocks in batch(jobs, "Загрузка баз", 15):
        for base in blocks:
            if base.get("infobase"):
                found.append({"server": info["server"], "port": info["port"],
                              "base": base.get("name", "?"),
                              "cluster": f"{info['cluster_name']} ({info['cluster_id'][:8]}...)",
                              "cluster_id": info["cluster_id"], "infobase_id": base["infobase"],
                              'cluster_host': info['cluster_host'], 'cluster_port': info['cluster_port']})
    found.sort(key=lambda row: (row["server"].casefold(), row["port"], row["base"].casefold()))
    return found, errors


def local_tool_candidates(item, versions):
    """Try managed tools first; discover local installs only if needed."""
    seen = set()
    for tools in managed_tools()[:1]:
        seen.add(str(Path(tools[1]).resolve()).casefold())
        yield tools
    wanted = item.get('version', '')
    mapping = dict(versions)
    mapping.update(find_all_rac())
    ordered = sorted(mapping, key=lambda version: (version == wanted, version_key(version)), reverse=True)
    for version in ordered:
        rac = mapping[version]
        ras = Path(rac).with_name('ras.exe')
        key = str(ras.resolve()).casefold()
        if key not in seen and Path(rac).is_file() and ras.is_file():
            seen.add(key)
            yield str(rac), str(ras), version


def select_local_tools(item, versions):
    """Compatibility helper for callers that only need the first candidate."""
    tools = next(local_tool_candidates(item, versions), None)
    if tools is None:
        raise RuntimeError('Встроенный RAS отсутствует и локальные ras.exe/rac.exe не найдены. '
                           'Обновите RAS в разделе «Установка 1С из ИТС» или установите компоненты администрирования 1С.')
    return tools


def collect_bases_local(items, versions, user, password, manager, notify, stop, runner):
    """Each bounded worker owns a RAS for one agent until all its bases are read."""
    targets = {}
    for item in items:
        key = (item["server"].casefold(), str(item["port"]))
        if key not in targets or (not targets[key].get("version") and item.get("version")):
            targets[key] = item
    found, errors = [], []
    credentials = [f"--cluster-user={user}", f"--cluster-pwd={password}"] if user or password else []

    def cancelled():
        return stop is not None and stop.is_set()

    def query(item):
        host = item["server"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        agent = f"{host}:{item['port']}"
        results, failures = [], []
        context = f"{agent} (локальный RAS)"
        try:
            if cancelled():
                return results, failures
            if not 0 < int(item["port"]) <= 65535:
                raise ValueError("Некорректный порт агента 1С в appsrvrs.lst")
            def report(text):
                notify(agent + ' · ' + text, 0, len(targets))
            with connect_with_fallback(manager, local_tool_candidates(item, versions), agent,
                    lambda rac, endpoint: runner([rac, 'cluster', 'list', endpoint]), report) as connection:
                rac, endpoint, version, output = connection
                context = f"{agent} (RAS {version})"
                clusters = parse_rac(output)
                for cluster in clusters:
                    if cancelled():
                        break
                    cluster_id = cluster.get("cluster")
                    if not cluster_id:
                        continue
                    try:
                        output = runner([rac, "infobase", "summary", "list",
                                         f"--cluster={cluster_id}", *credentials, endpoint])
                        for base in parse_rac(output):
                            if base.get("infobase"):
                                results.append({"server": item["server"], "port": item["port"],
                                                "base": base.get("name", "?"),
                                                "cluster": f"{cluster.get('name', '')} ({cluster_id[:8]}...)",
                                                "cluster_id": cluster_id, "infobase_id": base["infobase"],
                                                'cluster_host': cluster.get('host', ''), 'cluster_port': cluster.get('port', '')})
                    except Exception as exc:
                        if not cancelled():
                            failures.append(f"{context}, кластер {cluster_id}: {exc}")
        except SearchCancelled:
            pass
        except Exception as exc:
            if not cancelled():
                failures.append(f"{context}: {exc}\nПроверьте доступ к агенту 1С и совпадение версии RAS "
                                "с серверной платформой; версия в .lst берётся из описания.")
        return results, failures

    try:
        if not targets or cancelled():
            return found, errors
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
            pending = [executor.submit(query, item) for item in targets.values()]
            for done, future in enumerate(concurrent.futures.as_completed(pending), 1):
                if cancelled():
                    manager.close()
                    for waiting in pending:
                        waiting.cancel()
                    break
                results, failures = future.result()
                found.extend(results)
                errors.extend(failures)
                notify("Поиск баз через локальный RAS", done, len(targets))
    finally:
        manager.close()
    found.sort(key=lambda row: (row["server"].casefold(), row["port"], row["base"].casefold()))
    return found, errors
