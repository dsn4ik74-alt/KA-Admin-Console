"""Register an infobase through RAC; also an interactive script for this project.

No shell, generated command files, or stored credentials are used. The target
server is an agent endpoint (1540 etc.), never an inferred cluster/RAS port.
"""
import argparse
import contextlib
import getpass
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from uuid import UUID

from console_core import (discover_local_lst, find_all_rac, find_rac_best, parse_rac,
                          read_lst, local_tool_candidates)
from local_ras import LocalRasManager, SearchCancelled, decode_output, connect_with_fallback
from postgres_runner import OperationCancelled, stop_process


DBMS = {'Microsoft SQL Server': 'MSSQLServer', 'PostgreSQL': 'PostgreSQL',
        'IBM Db2': 'IBMDB2', 'Oracle Database': 'OracleDatabase'}
SECURITY = {'выключено': '0', 'только при установке соединения': '1', 'постоянно': '2'}
LOCALES = {'русский (Россия)': 'ru_RU', 'английский (США)': 'en_US',
           'русский': 'ru', 'английский': 'en'}
PARAMETERS = ('name', 'descr', 'security-level', 'db-server', 'dbms', 'db-name', 'db-user',
              'db-pwd', 'license-distribution', 'locale', 'date-offset', 'scheduled-jobs-deny')


def redact(text, secrets):
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        text = text.replace(secret, '••••')
    return text


def validate_parameters(parameters):
    result = dict(parameters)
    for key in PARAMETERS:
        value = result.get(key, '')
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError('Некорректное значение поля: ' + key)
    for key in ('name', 'db-server', 'db-name', 'locale'):
        result[key] = result.get(key, '').strip()
        if not result[key]:
            raise ValueError('Заполните поле: ' + key)
    if result['dbms'] not in DBMS.values():
        raise ValueError('Выберите тип СУБД')
    if result['security-level'] not in ('0', '1', '2'):
        raise ValueError('Выберите уровень защищённого соединения')
    if result['license-distribution'] not in ('allow', 'deny'):
        raise ValueError('Выберите режим выдачи лицензий')
    if result['scheduled-jobs-deny'] not in ('on', 'off'):
        raise ValueError('Выберите режим регламентных заданий')
    if result['date-offset'] not in ('0', '2000'):
        raise ValueError('Смещение дат: 0 или 2000')
    if not re.fullmatch(r'[A-Za-z]{2,3}(?:_[A-Za-z]{2})?', result['locale']):
        raise ValueError('Укажите язык (страну), например ru_RU или en_US')
    if type(result.get('create-database', False)) is not bool:
        raise ValueError('Создание БД должно задаваться флажком')
    return result


def create_command(rac, endpoint, cluster, parameters, user='', password=''):
    parameters = validate_parameters(parameters)
    cluster = str(UUID(cluster))
    args = [str(rac), 'infobase', 'create', '--cluster=' + cluster]
    if user or password:
        args += ['--cluster-user=' + user, '--cluster-pwd=' + password]
    args += ['--' + key + '=' + parameters.get(key, '') for key in PARAMETERS]
    if parameters.get('create-database'):
        args.append('--create-database')
    return args + [endpoint]


def run_rac(args, cancel_event, secrets=(), timeout=15):
    if cancel_event.is_set():
        raise OperationCancelled('Запрос 1С прерван')
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    started = time.monotonic()
    try:
        while True:
            if cancel_event.is_set():
                raise OperationCancelled('Запрос 1С прерван')
            if timeout is not None and time.monotonic() - started > timeout:
                raise RuntimeError('RAC не ответил за ' + str(timeout) + ' с')
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        output = decode_output(stdout)
        if process.returncode:
            detail = decode_output(stderr).strip() or output.strip() or 'без сообщения'
            raise RuntimeError('rac: код ' + str(process.returncode) + '. ' + redact(detail[-4000:], secrets))
        # Parsing must use the original text: a short password can also occur in
        # a UUID or option name. Only errors and emitted logs are redacted.
        return output
    finally:
        stop_process(process)
        process.stdout.close()
        process.stderr.close()


class ClusterClient:
    def __init__(self, target, versions, user='', password='', local=True, ras_port=1545,
                 event=lambda item: None, cancel_event=None, ib_user='', ib_password=''):
        self.target, self.versions = dict(target), dict(versions)
        self.user, self.password, self.local, self.ras_port = user, password, local, int(ras_port)
        self.event = event
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.ib_user, self.ib_password = ib_user, ib_password
        self.secrets = [password, ib_password]

    def emit(self, text, kind='message'):
        self.event(dict(kind=kind, text=redact(str(text), self.secrets)))

    def run(self, args, timeout=15):
        return run_rac(args, self.cancel_event, self.secrets, timeout)

    @contextlib.contextmanager
    def connect(self):
        host = self.target.get('server', '').strip()
        port = int(self.target.get('port', 0))
        if not host or host.startswith('-') or any(ord(char) < 33 for char in host) or not 1 <= port <= 65535:
            raise ValueError('Выберите сервер 1С с корректным портом агента')
        host = '[' + host + ']' if ':' in host and not host.startswith('[') else host
        if self.local:
            manager = LocalRasManager(self.cancel_event)
            try:
                with connect_with_fallback(manager, local_tool_candidates(self.target, self.versions), f'{host}:{port}',
                        lambda rac, endpoint: self.run([rac, 'cluster', 'list', endpoint]), self.emit,
                        warning=lambda text: self.emit(text, 'warning')) as connection:
                    rac, endpoint, version, clusters = connection
                    self._connection_clusters = (rac, endpoint, clusters)
                    self.emit(f'Сервер 1С: {host}:{port} · временный локальный RAS {version}', 'stage')
                    yield rac, endpoint
            except SearchCancelled as exc:
                raise OperationCancelled('Запрос 1С прерван') from exc
            finally:
                self._connection_clusters = None
                manager.close()
        else:
            if not 1 <= self.ras_port <= 65535:
                raise ValueError('Некорректный порт RAS')
            rac = self.versions.get(self.target.get('version')) or find_rac_best(self.versions)
            if not rac or not Path(rac).is_file():
                raise ValueError('Не найден rac.exe нужной версии')
            self.emit(f'Сервер 1С: {host}:{port} · RAS {host}:{self.ras_port}', 'stage')
            yield rac, f'{host}:{self.ras_port}'

    def credentials(self):
        return ['--cluster-user=' + self.user, '--cluster-pwd=' + self.password] if self.user or self.password else []

    def ib_credentials(self):
        return ['--infobase-user=' + self.ib_user, '--infobase-pwd=' + self.ib_password] if self.ib_user or self.ib_password else []

    def cluster_rows(self, rac, endpoint):
        cached = getattr(self, '_connection_clusters', None)
        if cached and cached[:2] == (rac, endpoint):
            self._connection_clusters = None
            return [row for row in parse_rac(cached[2]) if row.get('cluster')]
        return [row for row in parse_rac(self.run([rac, 'cluster', 'list', endpoint])) if row.get('cluster')]

    def summary(self, rac, endpoint, cluster):
        return [row for row in parse_rac(self.run([rac, 'infobase', 'summary', 'list', '--cluster=' + cluster,
                                                  *self.credentials(), endpoint])) if row.get('infobase')]

    def require_cluster(self, rac, endpoint, cluster):
        if not any(row['cluster'].casefold() == cluster for row in self.cluster_rows(rac, endpoint)):
            raise ValueError('Выбранный кластер больше не найден. Обновите список кластеров.')

    def require_base(self, rac, endpoint, cluster, infobase, expected_name):
        self.require_cluster(rac, endpoint, cluster)
        row = next((row for row in self.summary(rac, endpoint, cluster)
                    if row['infobase'].casefold() == infobase), None)
        if row is None or row.get('name', '').casefold() != expected_name.casefold():
            raise ValueError('Выбранная ИБ исчезла или изменилась. Обновите список баз перед операцией.')

    def clusters(self):
        with self.connect() as (rac, endpoint):
            return self.cluster_rows(rac, endpoint)

    def agent_version(self, rac, endpoint):
        output = self.run([rac, 'agent', 'version', endpoint])
        versions = set(re.findall(r'(?<!\d)8\.[35]\.\d+\.\d+(?!\d)', output))
        if len(versions) != 1:
            raise ValueError('Агент 1С не вернул однозначную версию платформы. Обновите список кластеров.')
        return versions.pop()

    def deployment_context(self, cluster):
        cluster = str(UUID(cluster))
        with self.connect() as (rac, endpoint):
            row = next((row for row in self.cluster_rows(rac, endpoint) if row['cluster'].casefold() == cluster), None)
            if row is None:
                raise ValueError('Выбранный кластер больше не найден. Обновите список кластеров.')
            return dict(row, platform_version=self.agent_version(rac, endpoint))

    def launch_context(self, cluster, infobase, expected_name):
        cluster, infobase = str(UUID(cluster)), str(UUID(infobase))
        with self.connect() as (rac, endpoint):
            row = next((row for row in self.cluster_rows(rac, endpoint) if row['cluster'].casefold() == cluster), None)
            if row is None:
                raise ValueError('Кластер выбранной базы больше не найден. Обновите список ИБ.')
            base = next((base for base in self.summary(rac, endpoint, cluster)
                         if base['infobase'].casefold() == infobase), None)
            if base is None or base.get('name', '').casefold() != expected_name.casefold():
                raise ValueError('Выбранная база исчезла или была переименована. Обновите список ИБ перед открытием.')
            return dict(cluster=dict(row, platform_version=self.agent_version(rac, endpoint)), base=base)

    def overview(self, preferred='', include_version=False):
        preferred = str(UUID(preferred)) if preferred else ''
        with self.connect() as (rac, endpoint):
            clusters = self.cluster_rows(rac, endpoint)
            if include_version:
                version = self.agent_version(rac, endpoint)
                clusters = [dict(row, platform_version=version) for row in clusters]
            selected = next((row['cluster'] for row in clusters if row['cluster'].casefold() == preferred), '')
            if not selected and len(clusters) == 1:
                selected = clusters[0]['cluster']
            return dict(clusters=clusters, cluster=selected,
                        bases=self.summary(rac, endpoint, selected) if selected else [])

    def bases(self, cluster):
        cluster = str(UUID(cluster))
        with self.connect() as (rac, endpoint):
            self.require_cluster(rac, endpoint, cluster)
            return self.summary(rac, endpoint, cluster)

    def set_jobs(self, rac, endpoint, cluster, infobase, denied):
        state = 'on' if denied else 'off'
        self.emit('Установка блокировки регламентных заданий: ' + state, 'stage')
        args = ['--cluster=' + cluster, '--infobase=' + infobase, *self.credentials(), *self.ib_credentials()]
        self.run([rac, 'infobase', 'update', *args, '--scheduled-jobs-deny=' + state, endpoint], timeout=30)
        actual = self.jobs_state(rac, endpoint, cluster, infobase)
        if actual != state:
            raise RuntimeError('Блокировка заданий не подтверждена: ожидалось ' + state +
                               ', сервер вернул ' + actual + '. ИБ не пересоздаётся.')
        self.emit('Проверено через infobase info: scheduled-jobs-deny=' + actual)
        return denied

    def jobs_state(self, rac, endpoint, cluster, infobase):
        args = ['--cluster=' + cluster, '--infobase=' + infobase, *self.credentials(), *self.ib_credentials()]
        rows = parse_rac(self.run([rac, 'infobase', 'info', *args, endpoint], timeout=30))
        row = next((row for row in rows if row.get('infobase', '').casefold() == infobase), None)
        actual = row.get('scheduled-jobs-deny', '').strip().casefold() if row else ''
        if actual not in ('on', 'off'):
            raise RuntimeError('Блокировка заданий не подтверждена: сервер не вернул допустимое '
                               'scheduled-jobs-deny для созданной ИБ. ИБ не пересоздаётся.')
        return actual

    def apply_scheduled_jobs(self, cluster, infobase, expected_name, denied):
        cluster, infobase = str(UUID(cluster)), str(UUID(infobase))
        if type(denied) is not bool:
            raise ValueError('Блокировка заданий должна задаваться флажком')
        with self.connect() as (rac, endpoint):
            self.require_base(rac, endpoint, cluster, infobase, expected_name)
            return self.set_jobs(rac, endpoint, cluster, infobase, denied)

    def drop(self, cluster, infobase, expected_name):
        cluster, infobase = str(UUID(cluster)), str(UUID(infobase))
        with self.connect() as (rac, endpoint):
            self.require_base(rac, endpoint, cluster, infobase, expected_name)
            self.emit('Удаление регистрации ИБ из кластера: ' + expected_name, 'stage')
            try:
                self.run([rac, 'infobase', 'drop', '--cluster=' + cluster, '--infobase=' + infobase,
                          *self.credentials(), *self.ib_credentials(), endpoint], timeout=None)
            except OperationCancelled as exc:
                raise OperationCancelled('Запрос удаления ИБ прерван. Проверьте список ИБ: сервер мог уже удалить регистрацию.') from exc
            self.emit('Регистрация ИБ удалена: ' + expected_name + '. База данных в СУБД сохранена.')
            return infobase

    def create(self, cluster, parameters):
        parameters = validate_parameters(parameters)
        cluster = str(UUID(cluster))
        self.secrets.append(parameters.get('db-pwd', ''))
        with self.connect() as (rac, endpoint):
            # The installed utility's own help is authoritative for its version.
            help_text = self.run([rac, 'help', 'infobase'])
            required = ['--' + key for key in PARAMETERS]
            if parameters.get('create-database'):
                required.append('--create-database')
            missing = [key for key in required if key not in help_text]
            if missing:
                raise ValueError('Версия rac не поддерживает параметры: ' + ', '.join(missing))
            clusters = parse_rac(self.run([rac, 'cluster', 'list', endpoint]))
            if not any(row.get('cluster', '').casefold() == cluster for row in clusters):
                raise ValueError('Выбранный кластер больше не найден. Обновите список кластеров.')
            summary = [rac, 'infobase', 'summary', 'list', '--cluster=' + cluster, *self.credentials(), endpoint]
            existing = parse_rac(self.run(summary))
            if any(row.get('name', '').casefold() == parameters['name'].casefold() for row in existing):
                raise ValueError('В этом кластере уже есть ИБ с таким именем')
            command = create_command(rac, endpoint, cluster, parameters, self.user, self.password)
            self.emit('Добавление ИБ ' + parameters['name'] + ' · ' + parameters['dbms'] + ' · ' +
                      parameters['db-server'] + ' / ' + parameters['db-name'], 'stage')
            requested = parameters['scheduled-jobs-deny']
            self.emit('Блокировка заданий передаётся при создании: scheduled-jobs-deny=' + requested)
            try:
                output = self.run(command, timeout=None)
            except OperationCancelled as exc:
                raise OperationCancelled('Запрос добавления ИБ прерван. Сервер мог успеть создать базу. '
                                         'Проверьте список ИБ перед повторным добавлением.') from exc
            self.emit(output.strip())
            rows = parse_rac(output)
            infobase = next((row.get('infobase') for row in rows if row.get('infobase')), '')
            if not infobase:
                try:
                    rows = parse_rac(self.run(summary))
                    infobase = next((row.get('infobase', '') for row in rows
                                     if row.get('name', '').casefold() == parameters['name'].casefold()), '')
                except Exception as exc:
                    self.emit('RAC завершился успешно, но UUID ИБ не прочитан: ' + str(exc))
            if not infobase:
                self.emit('Обновите список ИБ, чтобы проверить регистрацию. Повторное создание автоматически не выполняется.')
            settings_error, denied, access_required = '', None, False
            if infobase:
                action = 'проверить'
                try:
                    infobase = str(UUID(infobase))
                    self.emit('Проверка блокировки заданий после создания ИБ', 'stage')
                    actual = self.jobs_state(rac, endpoint, cluster, infobase)
                    if actual == requested:
                        denied = actual == 'on'
                        self.emit('Проверено после создания: scheduled-jobs-deny=' + actual +
                                  '. Дополнительное изменение свойств не требуется.')
                    else:
                        self.emit('После создания сервер вернул scheduled-jobs-deny=' + actual +
                                  '; требуется ' + requested + '. Применение настройки без повторного создания ИБ.')
                        action = 'изменить и проверить'
                        denied = self.set_jobs(rac, endpoint, cluster, infobase, requested == 'on')
                except OperationCancelled as exc:
                    raise OperationCancelled('ИБ уже добавлена: ' + parameters['name'] + ' · UUID: ' + infobase +
                                             '. Настройка заданий прервана; проверьте состояние ИБ. Не создавайте её повторно.') from exc
                except (ValueError, RuntimeError, OSError) as exc:
                    access_required = bool(re.search(r'недостаточно прав|insufficient (?:rights|permissions)|'
                                                      r'аутентификац|authentication|авторизац', str(exc), re.I))
                    settings_error = ('ИБ создана. Параметр scheduled-jobs-deny=' + requested +
                                      ' передан при создании, но не удалось ' + action + ' блокировку: ' +
                                      redact(str(exc), self.secrets))
                    if access_required:
                        settings_error += ('\nРаскройте «Авторизация ИБ…» и укажите пользователя самой ИБ с правами '
                                           'администрирования и аутентификацией 1С. Логин СУБД и права Windows его не заменяют. '
                                           'Фактическое состояние блокировки пока не подтверждено.')
                    self.emit(settings_error)
            else:
                settings_error = 'UUID созданной ИБ не прочитан; блокировка заданий не проверена. Обновите список ИБ.'
            self.emit('Команда добавления ИБ завершена: ' + parameters['name'])
            return dict(name=parameters['name'], infobase_id=infobase, cluster_id=cluster,
                        scheduled_jobs_deny=denied, settings_error=settings_error, jobs_access_required=access_required)


def choose(rows, label):
    if not rows:
        raise ValueError('Список серверов/кластеров пуст. Укажите сервер или обновите список.')
    if len(rows) == 1:
        return rows[0]
    for index, row in enumerate(rows, 1):
        print(str(index) + '. ' + label(row))
    index = int(input('Номер: ')) - 1
    if not 0 <= index < len(rows):
        raise ValueError('Некорректный номер')
    return rows[index]


def main():
    parser = argparse.ArgumentParser(description='Добавить информационную базу в кластер 1С')
    parser.add_argument('--server', help='Сервер агента; иначе выбор из локального appsrvrs.lst')
    parser.add_argument('--agent-port', type=int, default=1540)
    parser.add_argument('--version', default='')
    parser.add_argument('--cluster', default='')
    parser.add_argument('--cluster-user', default='')
    parser.add_argument('--infobase-user', default='', help='Пользователь ИБ для настройки заданий, если требуется')
    args = parser.parse_args()
    target = dict(server=args.server, port=args.agent_port, version=args.version)
    if not args.server:
        report = discover_local_lst()
        items = [dict(row, server=server) for path in report.current_paths
                 for server, rows in read_lst(path).items() for row in rows]
        target = choose(items, lambda row: str(row['server']) + ':' + str(row['port']))
        if args.version:
            target['version'] = args.version
    password = getpass.getpass('Пароль администратора кластера: ') if args.cluster_user else ''
    ib_password = getpass.getpass('Пароль пользователя ИБ: ') if args.infobase_user else ''
    client = ClusterClient(target, find_all_rac(), args.cluster_user, password, event=lambda item: print(item['text']),
                           ib_user=args.infobase_user, ib_password=ib_password)
    cluster = args.cluster or choose(client.clusters(), lambda row: row.get('name', '') + ' · ' + row['cluster'])['cluster']
    name = input('Имя ИБ: ').strip()
    print('СУБД: ' + ', '.join(DBMS.values()))
    parameters = {'name': name, 'descr': input('Описание: '),
                  'security-level': input('Защищённое соединение 0/1/2 [0]: ') or '0',
                  'db-server': input('Сервер баз данных: '), 'dbms': input('Тип СУБД [MSSQLServer]: ') or 'MSSQLServer',
                  'db-name': input('База данных: '), 'db-user': input('Пользователь сервера БД: '),
                  'db-pwd': getpass.getpass('Пароль пользователя БД: '),
                  'license-distribution': 'deny' if input('Разрешить выдачу лицензий [да]: ').lower() in ('нет', 'no') else 'allow',
                  'locale': input('Язык (страна) [ru_RU]: ') or 'ru_RU'}
    date_default = '2000' if parameters['dbms'] == 'MSSQLServer' else '0'
    parameters.update({
                  'date-offset': input('Смещение дат 0/2000 [' + date_default + ']: ') or date_default,
                  'create-database': input('Создать БД в случае отсутствия [нет]: ').lower() in ('да', 'yes'),
                  'scheduled-jobs-deny': 'on' if input('Заблокировать регламентные задания [нет]: ').lower() in ('да', 'yes') else 'off'})
    client.create(cluster, parameters)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt) as exc:
        raise SystemExit(str(exc))
