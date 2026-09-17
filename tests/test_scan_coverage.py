import importlib.util
import pathlib
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('scan_coverage', pathlib.Path(__file__).parents[1] / 'scripts' / 'scan_coverage.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class CoverageReports(unittest.TestCase):
    def test_java_compile_only_plan_does_not_run_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('<project/>')
            _, commands = scanner.plan(root, ['COMPILE'])
            self.assertEqual(['BUILD'], [stage for stage, _ in commands])
            self.assertNotIn(' test', commands[0][1])

    def test_java_coverage_cleans_stale_output_and_does_not_duplicate_configured_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('<artifactId>jacoco-maven-plugin</artifactId>')
            _, commands = scanner.plan(root, ['TEST', 'COVERAGE'])
            command = commands[0][1]
            self.assertIn(' clean test ', command)
            self.assertNotIn('prepare-agent', command)

    def test_node_test_without_coverage_does_not_request_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'package.json').write_text('{"scripts":{"test":"vitest"}}')
            _, commands = scanner.plan(root, ['TEST'])
            self.assertEqual(['TEST'], [stage for stage, _ in commands])
            self.assertNotIn('--coverage', commands[0][1])

    def test_jacoco_uses_line_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'jacoco.xml').write_text('<report><counter type="LINE" missed="3" covered="7"/></report>')
            self.assertEqual(70, scanner.coverage(root))

    def test_no_report_is_not_zero_or_perfect_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(scanner.coverage(pathlib.Path(directory)))

    def test_lcov_measures_line_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'lcov.info').write_text('LF:10\nLH:5\nend_of_record\nLF:10\nLH:10\nend_of_record\n')
            self.assertEqual(75, scanner.coverage(root))

    def test_archive_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('../escaped.py', 'bad')
            with self.assertRaises(ValueError):
                scanner.extract(archive, root / 'source')


if __name__ == '__main__':
    unittest.main()
