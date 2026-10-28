import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import account_creation as accounts


class PowerShellPreviewTests(unittest.TestCase):
    """Run the original identity functions in Windows PowerShell, without writes."""
    def test_unicode_company_legacy_email_duplicates_and_invalid_names(self):
        names = ('Иванов Иван Иванович "Компания"\nПетров Пётр старый@example.ru\n'
                 'Иванов Игорь\nОднослово\nСидоров-Орлов Юрий\n')
        rows = []
        accounts.run_account_batch(names, "EXAMPLE.RU", rows.append, preview=True)
        self.assertEqual([row["status"] for row in rows], ["ready", "ready", "duplicate", "error", "ready"])
        self.assertEqual(rows[0]["username"], "i.ivanov")
        self.assertEqual(rows[0]["company"], "Компания")
        self.assertEqual(rows[1]["email"], "p.petrov@example.ru")
        self.assertEqual(rows[4]["username"], "y.sidorov-orlov")
        self.assertTrue(all(row["password"] == "" for row in rows))

    def test_input_is_data_and_no_input_or_result_files_are_created(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = os.getcwd()
            os.chdir(folder)
            try:
                rows = []
                accounts.run_account_batch('Иванов Иван "$(throw 123) & ; Компания"', "example.ru",
                                           rows.append, preview=True)
                self.assertEqual(rows[0]["status"], "ready")
                self.assertEqual(rows[0]["company"], "$(throw 123) & ; Компания")
                self.assertEqual(list(Path(folder).iterdir()), [])
            finally:
                os.chdir(previous)

    def test_invalid_domain_fails_before_processing_any_user(self):
        rows = []
        with self.assertRaisesRegex(RuntimeError, "домен"):
            accounts.run_account_batch("Иванов Иван", "bad domain", rows.append, preview=True)
        self.assertEqual(rows, [])

    def test_windows_login_length_limit(self):
        rows = []
        accounts.run_account_batch("Оченьдлиннаяфамилияочень Иван", "example.ru", rows.append, preview=True)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("20", rows[0]["message"])


FAKE_FUNCTIONS = r'''
# This body must never run; only the whitelisted function definitions are used.
throw 'Top-level script was executed'
function ConvertTo-LatinName { param($Name) return $Name }
function Get-UserIdentity {
    param($FullName, $Domain)
    $user = ($FullName -split ' ')[0].ToLowerInvariant()
    return @{ User = $user; Email = "$user@$Domain" }
}
function Generate-Password { return 'Test-only-password' }
function User-Exists($username) { return $username -eq 'existing' }
function Create-User {
    param($username, $passwordPlain, $fullName, $description)
    if ($username -eq 'fail') { throw 'Simulated creation failure' }
    if ($description.Length -gt 48) { throw 'Description too long' }
    return [PSCustomObject]@{ Name = $username }
}
function Add-UserToRDP($username) {
    if ($username -eq 'partial') { throw 'Simulated RDP failure' }
}
'''


class PowerShellMockCreationTests(unittest.TestCase):
    def test_creation_existing_failure_and_rdp_partial_success(self):
        # The replacement source contains NO Windows account mutation commands.
        # Bypass only the administrator precheck so this in-memory simulation
        # runs with an ordinary test token. Production source remains unchanged.
        adapter = accounts.POWERSHELL_ADAPTER.replace("if (-not $preview) {", "if ($false) {", 1)
        rows = []
        with patch.object(accounts, "script_source", return_value=FAKE_FUNCTIONS), \
                patch.object(accounts, "POWERSHELL_ADAPTER", adapter):
            accounts.run_account_batch('New Person "A very long company name that will be truncated"\n'
                                       'Partial Person\nExisting Person\nFail Person',
                                       "example.ru", rows.append)
        grouped = {}
        for row in rows:
            grouped.setdefault(row["username"], []).append(row)
        self.assertEqual([row["status"] for row in grouped["new"]], ["created", "created_rdp"])
        self.assertEqual([row["status"] for row in grouped["partial"]], ["created", "created_no_rdp"])
        self.assertEqual(grouped["partial"][-1]["password"], "Test-only-password")
        self.assertEqual(grouped["existing"][-1]["status"], "exists")
        self.assertEqual(grouped["existing"][-1]["password"], "")
        self.assertEqual(grouped["fail"][-1]["status"], "error")
        self.assertEqual(grouped["fail"][-1]["password"], "")


class TransportTests(unittest.TestCase):
    def fake_process(self, output, code=0):
        process = MagicMock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(output)
        process.poll.return_value = code
        process.wait.return_value = code
        return process

    def test_partial_result_is_delivered_before_process_failure(self):
        row = dict(line=1, status="created", password="test-password")
        process = self.fake_process(json.dumps({"kind": "row", "row": row}) + "\n", 1)
        rows = []
        with patch.object(accounts.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "неполным ответом"):
                accounts.run_account_batch("Иванов Иван", "example.ru", rows.append)
        self.assertEqual(rows, [row])

    def test_stall_timeout_kills_only_its_child(self):
        process = self.fake_process("")
        process.poll.return_value = None
        with patch.object(accounts.subprocess, "Popen", return_value=process), \
                patch.object(accounts.time, "monotonic", side_effect=[0, 301]):
            with self.assertRaisesRegex(RuntimeError, "Превышено время"):
                accounts.run_account_batch("Иванов Иван", "example.ru", lambda row: None)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_input_is_not_in_process_arguments(self):
        process = self.fake_process('{"kind":"done","count":1}\n')
        with patch.object(accounts.subprocess, "Popen", return_value=process) as start:
            accounts.run_account_batch("Иванов Иван", "example.ru", lambda row: None)
        args = start.call_args.args[0]
        self.assertIn("-EncodedCommand", args)
        self.assertNotIn("Иванов Иван", " ".join(args))
        self.assertLess(len(" ".join(args)), 32767)


if __name__ == "__main__":
    unittest.main()
