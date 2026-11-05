"""No-runtime regression tests: execute only our fixed architecture guard and two path assignments.

Never source start-bionic.sh: that starts services and loads proprietary SDKs.
"""
import ast
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest

STARTUP = Path(__file__).resolve().parents[1] / 'start-bionic.sh'
EXPECTED = (
    'export TUTK_LIB_DIR="${TUTK_LIB_DIR:-/app/libs/x86_64}"',
    'export LD_LIBRARY_PATH="$TUTK_LIB_DIR:/data/data/com.termux/files/usr/lib"',
)
GUARD = 'case "${TUTK_ARCH:-x86_64}" in\n  x86_64) ;;\n  *) printf \'%s\\n\' \'Unsupported TUTK_ARCH: this image requires x86_64\' >&2; exit 64 ;;\nesac'
TERMUX = '/data/data/com.termux/files/usr/lib'

class StartupLibraryPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = STARTUP.read_text()
        cls.assignments = tuple(line for line in cls.source.splitlines()
                                if line.startswith(('export TUTK_LIB_DIR=', 'export LD_LIBRARY_PATH=')))
        if cls.assignments != EXPECTED:
            raise AssertionError('Unexpected startup assignment; refuse to execute upstream startup code')
        cls.guard = cls.source.split('# BEGIN SDK ARCH GUARD\n', 1)[1].split('\n# END SDK ARCH GUARD', 1)[0]
        if cls.guard != GUARD:
            raise AssertionError('Unexpected guard; refuse to execute startup code')
        cls.bash = shutil.which('bash')
        if cls.bash is None:
            raise unittest.SkipTest('bash is required for assignment-only shell tests')

    def resolve(self, **overrides):
        # Constant assignments plus printf only; no upstream startup body/import.
        code = self.guard + '\n' + '\n'.join(self.assignments) + '\nprintf "%s\\0%s" "$TUTK_LIB_DIR" "$LD_LIBRARY_PATH"'
        result = subprocess.run([self.bash, '--noprofile', '--norc', '-c', code],
                                env={'PATH': '/usr/bin:/bin', **overrides},
                                check=True, capture_output=True, timeout=5)
        return tuple(result.stdout.decode().split('\0'))

    def test_default_amd64_unchanged(self):
        self.assertEqual(self.resolve(), ('/app/libs/x86_64', '/app/libs/x86_64:' + TERMUX))
    def test_arm64_arch_default(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.resolve(TUTK_ARCH='arm64-v8a')
        self.assertEqual(error.exception.returncode, 64)
    def test_other_explicit_arch(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve(TUTK_ARCH='armeabi-v7a')
    def test_explicit_directory_wins(self):
        self.assertEqual(self.resolve(TUTK_ARCH='x86_64', TUTK_LIB_DIR='/custom/sdk'), ('/custom/sdk', '/custom/sdk:' + TERMUX))
    def test_custom_directory_does_not_bypass_arch_guard(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.resolve(TUTK_ARCH='arm64-v8a', TUTK_LIB_DIR='/custom/sdk')
        self.assertEqual(error.exception.returncode, 64)
        self.assertEqual(error.exception.stdout, b'')
    def test_guard_precedes_all_startup_side_effects(self):
        self.assertLess(self.source.index('# END SDK ARCH GUARD'), self.source.index('mkdir -p'))
        self.assertLess(self.source.index('# END SDK ARCH GUARD'), self.source.index('from lib_extract import'))
    def test_arch_value_is_not_evaluated_or_echoed(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.resolve(TUTK_ARCH='$(printf should-not-run)')
        self.assertEqual(error.exception.stderr, b'Unsupported TUTK_ARCH: this image requires x86_64\n')
    def test_empty_values_use_safe_default(self):
        self.assertEqual(self.resolve(TUTK_ARCH='', TUTK_LIB_DIR='')[0], '/app/libs/x86_64')
    def test_spaces_are_preserved_not_split(self):
        self.assertEqual(self.resolve(TUTK_LIB_DIR='/custom sdk/arm64')[0], '/custom sdk/arm64')
    def test_environment_value_not_reevaluated_as_shell(self):
        value='/custom/$(printf should-not-run)'
        self.assertEqual(self.resolve(TUTK_LIB_DIR=value)[0], value)
    def test_no_empty_loader_component_or_inherited_search_path(self):
        _, path=self.resolve(TUTK_LIB_DIR='', LD_LIBRARY_PATH='/synthetic/unused:')
        self.assertEqual(path, '/app/libs/x86_64:' + TERMUX)
        self.assertNotIn('', path.split(':'))
    def test_provisioner_uses_resolved_directory(self):
        line=next(line for line in self.source.splitlines() if line.startswith('python3 -c ') and 'provision' in line)
        snippet=shlex.split(line)[2]
        tree=ast.parse(snippet)
        call=next(node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='provision')
        self.assertEqual(ast.unparse(call.args[0]), "os.environ['TUTK_LIB_DIR']")
    def test_load_probe_uses_resolved_directory(self):
        snippet=self.source.split("python3 - <<'PY'",1)[1].split('\n',1)[1].split('\nPY\n',1)[0]
        tree=ast.parse(snippet)
        assignment=next(node for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='d' for t in node.targets))
        self.assertEqual(ast.unparse(assignment.value), "os.environ['TUTK_LIB_DIR']")

if __name__ == '__main__':
    unittest.main()

