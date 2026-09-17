import importlib.util
import os
import pathlib
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('scan_coverage', pathlib.Path(__file__).parents[1] / 'scripts' / 'scan_coverage.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class CoverageReports(unittest.TestCase):
    def test_maven_image_matches_bundle_java_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('<project><properties><java.version>21</java.version></properties></project>')
            image, _ = scanner.plan(root, ['TEST'])
            self.assertEqual('maven:3.9.9-eclipse-temurin-21', image)

    def test_maven_image_defaults_to_java_17_when_manifest_is_unspecified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('<project/>')
            image, _ = scanner.plan(root, ['TEST'])
            self.assertEqual('maven:3.9.9-eclipse-temurin-17', image)

    def test_node_image_matches_package_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'package.json').write_text('{"engines":{"node":">=20"},"scripts":{"test":"vitest"}}')
            image, _ = scanner.plan(root, ['TEST'])
            self.assertEqual('node:20-bookworm-slim', image)

    def test_python_image_matches_pyproject_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pyproject.toml').write_text('[project]\nrequires-python = ">=3.11"\n')
            (root / 'test_app.py').write_text('def test_ok(): assert True\n')
            image, _ = scanner.plan(root, ['TEST'])
            self.assertEqual('python:3.11-slim', image)

    def test_container_uses_runner_identity_instead_of_root(self):
        args = scanner.container_identity_args()
        uid = getattr(os, 'getuid', lambda: 1000)()
        gid = getattr(os, 'getgid', lambda: 1000)()
        self.assertEqual(str(uid) + ':' + str(gid), args[1])

    def test_kong_without_language_manifest_gets_real_validation_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            image, commands = scanner.config_plan(pathlib.Path(directory), ['TEST', 'COVERAGE'], 'KONG_GATEWAY_SERVICE')
            self.assertEqual('python:3.12-slim', image)
            self.assertEqual('TEST', commands[0][0])
            self.assertIn('validate_config_bundle.py KONG', commands[0][1])

    def test_apigee_alias_gets_bundle_validation_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            _, commands = scanner.config_plan(pathlib.Path(directory), ['TEST'], 'APIGEE_PROXY')
            self.assertIn('validate_config_bundle.py APIGEE', commands[0][1])

    def test_java_compile_only_plan_does_not_run_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('<project/>')
            _, commands = scanner.plan(root, ['COMPILE'])
            self.assertEqual(['BUILD'], [stage for stage, _ in commands])
            self.assertNotIn(' test', commands[0][1])

    def test_java_plugin_declaration_without_prepare_agent_gets_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('<artifactId>jacoco-maven-plugin</artifactId>')
            _, commands = scanner.plan(root, ['TEST', 'COVERAGE'])
            command = commands[0][1]
            self.assertIn('prepare-agent', command)

    def test_java_active_prepare_agent_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'pom.xml').write_text('''<project><build><plugins><plugin>
              <artifactId>jacoco-maven-plugin</artifactId><executions><execution><goals>
              <goal>prepare-agent</goal></goals></execution></executions>
              </plugin></plugins></build></project>''')
            _, commands = scanner.plan(root, ['TEST', 'COVERAGE'])
            self.assertNotIn(':prepare-agent', commands[0][1])
            self.assertIn(':report', commands[0][1])

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

    def test_disposable_workspace_is_writable_by_container_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            nested = root / 'src' / 'main'
            nested.mkdir(parents=True)
            source = nested / 'Application.java'
            source.write_text('class Application {}')
            root.chmod(0o700)
            nested.chmod(0o500)
            source.chmod(0o400)
            scanner.make_workspace_writable(root)
            self.assertTrue(root.stat().st_mode & 0o002)
            self.assertTrue(nested.stat().st_mode & 0o002)
            self.assertTrue(source.stat().st_mode & 0o002)

    def test_failed_test_reports_can_be_collected_before_error_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = root / 'output'
            project = root / 'project'
            report = project / 'target' / 'surefire-reports' / 'TEST-AppTest.xml'
            report.parent.mkdir(parents=True)
            report.write_text('<testsuite><testcase name="passes"/><testcase name="fails"><failure/></testcase></testsuite>')
            output.mkdir()
            scanner.collect_reports(project, output)
            self.assertEqual(1, scanner.test_counts(output)['passed'])
            self.assertEqual(1, scanner.test_counts(output)['failed'])


if __name__ == '__main__':
    unittest.main()
