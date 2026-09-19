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


class RunDetails(unittest.TestCase):
    def test_suites_and_failed_cases_come_from_the_junit_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'test-results.xml').write_text(
                '<testsuites><testsuite>'
                '<testcase classname="tests/a.test.ts" name="one" time="0.5"/>'
                '<testcase classname="tests/a.test.ts" name="two" time="0.25"><failure message="boom"/></testcase>'
                '<testcase classname="tests/b.test.ts" name="three"><skipped/></testcase>'
                '</testsuite></testsuites>')
            suites, failures = scanner.suite_details(root)
            self.assertEqual(['tests/a.test.ts', 'tests/b.test.ts'], [s['name'] for s in suites])
            self.assertEqual(dict(name='tests/a.test.ts', total=2, passed=1, failed=1, errors=0, skipped=0, seconds=0.75), suites[0])
            self.assertEqual([dict(suite='tests/a.test.ts', name='two', message='boom')], failures)

    def test_maven_summary_line_gives_the_counts_when_there_is_no_report(self):
        log = ('[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.8 s - in a.B\n'
               '[INFO] Results:\n[INFO] Tests run: 79, Failures: 1, Errors: 0, Skipped: 2\n')
        self.assertEqual(dict(total=79, passed=76, failed=1, errors=0, skipped=2), scanner.log_counts(log))

    def test_vitest_jest_and_pytest_summaries(self):
        self.assertEqual(dict(total=19, passed=18, failed=1, errors=0, skipped=0),
                         scanner.log_counts('\x1b[2m      Tests \x1b[22m  1 failed | 18 passed (19)\n'))
        self.assertEqual(dict(total=6, passed=5, failed=1, errors=0, skipped=0), scanner.log_counts('Tests:       1 failed, 5 passed, 6 total\n'))
        self.assertEqual(dict(total=7, passed=5, failed=1, errors=1, skipped=0), scanner.log_counts('==== 1 failed, 5 passed, 1 error in 0.42s ====\n'))

    def test_a_log_without_a_summary_reports_nothing_instead_of_inventing_counts(self):
        self.assertIsNone(scanner.log_counts('compiled successfully\n'))

    def test_logs_are_kept_without_colour_codes_and_within_the_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            (output / 'test.log').write_text('\x1b[31mred\x1b[0m ' + 'x' * (scanner.LOG_LIMIT + 50))
            log = scanner.read_logs(output)[0]
            self.assertEqual('test', log['stage'])
            self.assertNotIn('\x1b', log['text'])
            self.assertEqual(scanner.LOG_LIMIT, len(log['text']))

    def test_vitest_projects_also_get_junit_and_lcov(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'package.json').write_text('{"scripts":{"test":"vitest run"},"devDependencies":{"vitest":"^2"}}')
            command = scanner.node_test_command(root, {'test': 'vitest run'}, True)
            self.assertIn('--reporter=junit', command)
            self.assertIn('--coverage.reporter=lcov', command)
            (root / 'package.json').write_text('{"scripts":{"test":"jest"},"devDependencies":{"jest":"^29"}}')
            self.assertEqual('npm test -- --coverage', scanner.node_test_command(root, {'test': 'jest'}, True))
            self.assertEqual('npm test', scanner.node_test_command(root, {'test': 'jest'}, False))


class FinalEvent(unittest.TestCase):
    def test_the_closing_event_carries_everything_when_the_service_accepts_it(self):
        sent = []
        original = scanner.event
        scanner.event = lambda stage, **details: sent.append((stage, sorted(details)))
        try:
            scanner.send_final('COMPLETE', dict(tests=None, suites=[], logs=[]), dict(tests=None))
        finally:
            scanner.event = original
        self.assertEqual([('COMPLETE', ['logs', 'suites', 'tests'])], sent)

    def test_a_refused_closing_event_is_retried_with_the_essentials_only(self):
        import urllib.error
        sent = []
        original = scanner.event

        def refuse_the_big_one(stage, **details):
            sent.append(sorted(details))
            if 'logs' in details:
                raise urllib.error.HTTPError('https://x', 500, 'Internal Server Error', {}, None)

        scanner.event = refuse_the_big_one
        try:
            scanner.send_final('COMPLETE', dict(tests=None, suites=[], logs=[]), dict(tests=None))
        finally:
            scanner.event = original
        self.assertEqual([['logs', 'suites', 'tests'], ['tests']], sent)


if __name__ == '__main__':
    unittest.main()
