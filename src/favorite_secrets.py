"""Encrypt favorite credentials with Windows DPAPI for the current user."""
import base64
import binascii
import ctypes
from ctypes import wintypes
import json
import os


class CredentialError(ValueError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != 'nt':
        raise CredentialError('Для сохранения паролей требуется Windows DPAPI.')
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    try:
        # No LOCAL_MACHINE flag: credentials belong to this Windows account.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            action = 'расшифровать' if decrypt else 'защитить'
            raise CredentialError(f'Windows не удалось {action} пароли избранного (код {ctypes.get_last_error()}). '
                                  'Используйте ту же учётную запись Windows или сохраните подключение заново.')
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def protect(credentials):
    payload = json.dumps(credentials, ensure_ascii=False).encode('utf-8')
    return dict(scheme='dpapi-user-v1', data=base64.b64encode(_crypt(payload)).decode('ascii'))


def unprotect(envelope):
    try:
        if envelope.get('scheme') != 'dpapi-user-v1':
            raise ValueError('scheme')
        payload = base64.b64decode(envelope['data'], validate=True)
        result = json.loads(_crypt(payload, decrypt=True).decode('utf-8'))
        if not isinstance(result, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                  for key, value in result.items()):
            raise ValueError('payload')
        return result
    except CredentialError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, binascii.Error):
        raise CredentialError('Не удалось прочитать сохранённые пароли. Сохраните подключение заново.') from None
