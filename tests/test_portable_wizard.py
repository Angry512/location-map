"""Real Windows entry-point checks in a disposable copy; never opens a phone."""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

WORK = Path(__file__).resolve().parent
KIT = WORK.parent


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    system = Path(os.environ['SystemRoot']) / 'System32'
    cmd = system / 'cmd.exe'
    powershell = system / 'WindowsPowerShell/v1.0/powershell.exe'
    passed = []
    with TemporaryDirectory(prefix='wizard-portable-check-', dir=WORK) as temporary:
        scratch = Path(temporary).resolve()
        if scratch.parent != WORK:
            raise RuntimeError('Test files must stay directly inside the workspace.')
        copy = scratch / 'USB kit (space & copy)'
        copy.mkdir()
        for name in ('START HERE.cmd', 'READ ME.txt'):
            shutil.copy2(KIT / name, copy / name)
        shutil.copytree(KIT / 'Support', copy / 'Support',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        unrelated = scratch / 'Unrelated working directory'
        unrelated.mkdir()
        env = os.environ.copy()
        env['PATH'] = os.pathsep.join(map(str, (system, powershell.parent)))
        env['LOCALAPPDATA'] = str(scratch / 'New Windows profile')
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONUSERBASE'):
            env[key] = str(unrelated / 'not an installed Python')

        def run(label, expected=0, contains='USB package check passed'):
            starter = copy / 'START HERE.cmd'
            result = subprocess.run(
                f'"{cmd}" /d /s /c ""{starter}" -Check"', executable=cmd,
                cwd=unrelated, env=env, capture_output=True, text=True,
                errors='replace', timeout=180)
            output = result.stdout + result.stderr
            if result.returncode != expected or contains not in output:
                raise AssertionError(f'{label}: exit {result.returncode}; {output[-1800:]}')
            if 'Connect and unlock' in output or 'Downloading' in output:
                raise AssertionError('An offline check entered the installation flow.')
            passed.append(label)
            print('PASS:', label, flush=True)
            return output

        run('Fresh local runtime, foreign working directory, spaces and ampersand, hostile Python environment')
        run('Reused local runtime with the same isolated environment')

        support = copy / 'Support'
        runtime_hash = (support / 'runtime.sha256').read_text().strip()
        runtime = Path(env['LOCALAPPDATA']) / 'LocationMap/Runtime' / runtime_hash[:20]
        license_file = runtime / 'LICENSE.txt'
        license_original = license_file.read_bytes()
        license_file.write_bytes(b'damaged cached file')
        run('Corrupt cached runtime is replaced before the worker starts')
        if license_file.read_bytes() != license_original:
            raise AssertionError('Cache repair did not restore the original runtime file.')
        unwanted = runtime / 'unexpected extra module.py'
        unwanted.write_text('# This unrecognized file must be removed by cache repair.\n')
        run('Unexpected cached files are rejected and repaired')
        if unwanted.exists():
            raise AssertionError('Unrecognized cached files were reused.')

        moved = copy / 'Support temporarily absent'
        support.rename(moved)
        try:
            run('Missing Support folder gives an actionable failure', 1, 'Support folder is missing')
        finally:
            moved.rename(support)

        archive = support / 'Runtime.zip'
        with archive.open('ab') as stream:
            stream.write(b'corrupted copy')
        try:
            run('Damaged runtime archive rejected even with a populated cache', 1, 'Setup check failed')
        finally:
            shutil.copyfile(KIT / 'Support/Runtime.zip', archive)

        shortcut = support / 'Phone/Location Map.shortcut'
        original = shortcut.read_bytes()
        shortcut.write_bytes(b'corrupt shortcut')
        try:
            run('Damaged signed shortcut rejected before any phone access', 2, 'signed Shortcuts')
        finally:
            shortcut.write_bytes(original)

        payload = support / 'Phone/Location-Map-source.zip'
        saved = payload.with_suffix('.temporarily-absent')
        payload.rename(saved)
        try:
            output = run('Missing app archive explains the kit problem', 2, 'setup files')
            if 'Reconnect the iPhone' in output:
                raise AssertionError('A missing package file was misreported as a phone connection problem.')
        finally:
            saved.rename(payload)

        backend = support / 'Installer/wizard_backend.py'
        saved_backend = backend.with_suffix('.temporarily-absent')
        backend.rename(saved_backend)
        try:
            run('Worker startup failure produces an actionable message', 1, 'setup tools could not start')
        finally:
            saved_backend.rename(backend)

        # Exercise the actual C# worker with an inert local Python child.
        # This tests cancellation without starting the phone installer.
        fake = scratch / 'inert_child.py'
        fake.write_text('import json,os,time\nprint(json.dumps({"pid":os.getpid()}),flush=True)\ntime.sleep(90)\n')
        source = (support / 'Setup.ps1').read_text(encoding='utf-8-sig')
        match = re.search(r"-TypeDefinition @'\r?\n(.*?)\r?\n'@", source, re.S)
        if not match:
            raise AssertionError('Cannot find the actual Windows worker implementation.')
        csharp = scratch / 'worker.cs'
        csharp.write_text(match.group(1), encoding='utf-8')
        python = Path(env['LOCALAPPDATA']) / 'LocationMap/Runtime' / runtime_hash[:20] / 'python.exe'
        harness = scratch / 'cancel-worker.ps1'
        harness.write_text(
            "$ErrorActionPreference='Stop'\n"
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing,System.IO.Compression,System.IO.Compression.FileSystem\n"
            "Add-Type -ReferencedAssemblies @('System','System.Core','System.Drawing','System.Windows.Forms','System.IO.Compression','System.IO.Compression.FileSystem') -TypeDefinition ([IO.File]::ReadAllText(" + ps_quote(csharp) + "))\n"
            "$child=[LocationMapSetup.Backend]::new(" + ps_quote(python) + ',' + ps_quote(fake) + ",$false)\n"
            "$childId=0; $line=''; $clock=[Diagnostics.Stopwatch]::StartNew()\n"
            "try { while($clock.Elapsed.TotalSeconds -lt 15 -and !$childId) { while($child.Lines.TryDequeue([ref]$line)) { $childId=($line|ConvertFrom-Json).pid }; Start-Sleep -Milliseconds 50 } } finally { $child.Dispose() }\n"
            "if(!$childId){throw 'The inert test child did not report its ID'}\n"
            "$clock.Restart(); while((Get-Process -Id $childId -ErrorAction SilentlyContinue) -and $clock.Elapsed.TotalSeconds -lt 5){Start-Sleep -Milliseconds 50}\n"
            "if(Get-Process -Id $childId -ErrorAction SilentlyContinue){throw 'Cancel left the owned child running'}\n"
            "'PASS: Cancel stopped only the installer worker'\n",
            encoding='utf-8-sig')
        result = subprocess.run([str(powershell), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                                cwd=unrelated, env=env, capture_output=True, text=True, errors='replace', timeout=45)
        if result.returncode or 'PASS:' not in result.stdout:
            raise AssertionError('Worker cancellation: ' + (result.stdout + result.stderr)[-1800:])
        passed.append('Cancel terminates the owned worker without phone access')
        print(result.stdout.strip(), flush=True)

    (WORK / 'portable-wizard-results.json').write_text(json.dumps({'passed': passed, 'phone_accessed': False}, indent=2))
    print(f'{len(passed)} portable Windows checks passed; no phone was contacted.')


if __name__ == '__main__':
    main()
