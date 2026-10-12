"""P151: passive conversation-to-outcome evaluation (offline, read-only, synthetic fixtures only)."""
import hashlib
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from village import outcomes as oc
from village.artifacts import ArtifactStore
from village.coordinator import CoordinationStore

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TASK = 'aaaaaaaaaaa1'
TASK_IDLE = 'bbbbbbbbbbb2'
ART_INDEPENDENT = 'ccccccccccc3'
ART_SELF = 'ddddddddddd4'


def msg(mid, sender, recipient, content, ts, source='direct', reply_to=None):
    return dict(id=mid, source=source, sender=sender, recipient=recipient, reply_to=reply_to, content=content, timestamp=ts)


class Fixture(unittest.TestCase):
    """A real coordination.sqlite3 and artifacts.sqlite3 (schema from the stores), synthetic rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coord = root / 'coordination.sqlite3'
        self.arts = root / 'artifacts.sqlite3'
        self.events = root / 'events.jsonl'
        CoordinationStore(self.coord)
        ArtifactStore(self.arts)
        self.messages = [
            msg('m1', '02-explorer', '07-methodologist', f'Can you take task {TASK}?', '2026-10-12T08:00:00+00:00'),
            msg('m2', '07-methodologist', '02-explorer', f'I will claim {TASK} now.', '2026-10-12T08:05:00+00:00', reply_to='m1'),
            msg('m3', '04-artisan', '01-king', f'Artifact {ART_INDEPENDENT} is ready for review.', '2026-10-12T08:10:00+00:00'),
            msg('m4', '04-artisan', '01-king', f'Artifact {ART_SELF} is ready too.', '2026-10-12T08:11:00+00:00'),
            msg('m5', '03-librarian', '08-logician', f'Status of {TASK_IDLE}?', '2026-10-12T08:20:00+00:00'),
            msg('m6', '08-logician', '03-librarian', 'Thanks, noted.', '2026-10-12T08:30:00+00:00'),
            msg('m7', 'operator', '01-king', f'human signal about {TASK}', '2026-10-12T08:40:00+00:00', source='organic'),
            msg('m8', '01-king', None, 'Jour fixe at 14:00.', '2026-10-12T08:50:00+00:00', source='board'),
        ]
        with closing(sqlite3.connect(self.coord)) as c, c:
            c.executemany('INSERT INTO inbox_messages (id, source, sender, recipient, reply_to, content, timestamp, created_at) '
                          'VALUES (?,?,?,?,?,?,?,?)',
                          [(m['id'], m['source'], m['sender'], m['recipient'], m['reply_to'], m['content'], m['timestamp'],
                            m['timestamp']) for m in self.messages])
            task_cols = 'id, title, success_criterion, author, owner, status, evidence, completion_verified, created_at, updated_at, blockers'
            c.execute(f'INSERT INTO tasks ({task_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (TASK, 'Disk check', 'df output', '02-explorer', '07-methodologist', 'active', None, 0,
                       '2026-10-12T07:00:00+00:00', '2026-10-12T09:00:00+00:00', None))
            c.execute(f'INSERT INTO tasks ({task_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (TASK_IDLE, 'Idle', 'x', '03-librarian', None, 'open', None, 0,
                       '2026-10-11T07:00:00+00:00', '2026-10-12T06:00:00+00:00', 'waiting for mount access'))
            c.execute('INSERT INTO task_revisions (task_id, revision, actor, action, owner, status, timestamp) VALUES (?,?,?,?,?,?,?)',
                      (TASK, 2, '07-methodologist', 'claim', '07-methodologist', 'active', '2026-10-12T08:06:00+00:00'))
            c.execute('INSERT INTO message_receipts (message_id, agent_id, delivered, delivered_at, acknowledged, acknowledged_at) '
                      'VALUES (?,?,1,?,1,?)', ('m6', '03-librarian', '2026-10-12T08:31:00+00:00', '2026-10-12T08:31:00+00:00'))
        with closing(sqlite3.connect(self.arts)) as c, c:
            for aid, owner in ((ART_INDEPENDENT, '04-artisan'), (ART_SELF, '04-artisan')):
                c.execute('INSERT INTO artifacts (artifact_id, owner, task_id, file_path, sha256, status, required_resources_json, '
                          'provenance_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (aid, owner, TASK, f'/x/{aid}.sh', 'f' * 64, 'claimed_success', '{}', '{}',
                           '2026-10-12T08:00:00+00:00', '2026-10-12T08:00:00+00:00'))
            ver = 'INSERT INTO artifact_verifications (verification_id, artifact_id, verifier, verdict_type, passed, execution_time_ms, ' \
                  'exit_code, output_excerpt, recorded_sha256, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?)'
            c.execute(ver, ('v1', ART_INDEPENDENT, '01-king', 'reproduced', 1, 10, 0, 'OK', 'f' * 64, '2026-10-12T09:00:00+00:00'))
            c.execute(ver, ('v2', ART_SELF, '04-artisan', 'reproduced', 1, 10, 0, 'OK', 'f' * 64, '2026-10-12T09:00:00+00:00'))
        self.events.write_text('\n'.join(json.dumps(e) for e in [
            {'event_id': 'e1', 'agent': '03-librarian', 'event': 'command_result', 'timestamp': '2026-10-12T08:32:00+00:00'},
            {'event_id': 'e2', 'agent': '03-librarian', 'event': 'direct_message', 'timestamp': '2026-10-12T08:31:30+00:00'},
        ]) + '\n')

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, **kw):
        return oc.run(self.coord, self.arts, self.events, now=NOW, **kw)

    def by_id(self):
        data = oc.load_databases(self.coord, self.arts)
        rows, status = oc.load_events(self.events)
        return {r['id']: r for r in oc.analyse(data['messages'], data, rows, status, now=NOW)['messages']}


class LinkTests(Fixture):
    def test_changed_task_status_after_an_explicit_reference_is_task_activity(self):
        rows = self.by_id()
        self.assertEqual(rows['m1']['outcome'], 'verified')  # TASK also has an independently verified artifact
        self.assertIn({'kind': 'task_ref', 'id': TASK}, rows['m1']['links'])
        self.assertIn({'kind': 'reply', 'id': 'm1'}, rows['m2']['links'])

    def test_only_independent_verification_counts_and_competing_artifacts_stay_apart(self):
        rows = self.by_id()
        self.assertEqual(rows['m3']['outcome'], 'verified')
        self.assertEqual(rows['m4']['outcome'], 'none')  # verified by its own owner: a claim, not a verification

    def test_reference_without_any_later_change_is_none_and_missing_ids_are_unknown(self):
        rows = self.by_id()
        self.assertEqual(rows['m5']['outcome'], 'none')
        self.assertEqual(rows['m8']['outcome'], 'unknown')
        self.assertEqual(rows['m8']['links'], [])

    def test_ack_then_own_action_is_only_a_candidate_link(self):
        rows = self.by_id()
        # m6 went to 03-librarian, who acknowledged it 08:31 and ran a command 08:32;
        # sending a message is not counted as an action.
        self.assertEqual(rows['m6']['outcome'], 'candidate_action')
        self.assertEqual(rows['m6']['candidate']['event_id'], 'e1')
        self.assertEqual(rows['m6']['candidate']['kind'], 'candidate_link')

    def test_signals_from_outside_are_left_out(self):
        self.assertNotIn('m7', self.by_id())

    def test_report_figures_and_coverage(self):
        r = self.report()
        self.assertEqual(r['schema_version'], oc.SCHEMA)
        self.assertEqual(r['metrics']['messages_sent'], 7)
        self.assertEqual(r['metrics']['verified_outcomes_per_100_messages']['numerator'], 3)  # m1, m2 (both name TASK), m3
        self.assertEqual(r['evidence_coverage'], {'fully_linked': 3, 'explicit_only': 2, 'candidate_only': 1, 'unlinked': 1})
        self.assertEqual(r['metrics']['unresolved_blocker_age_hours']['count'], 1)
        self.assertEqual(r['metrics']['unresolved_blocker_age_hours']['max'], 6.0)
        latency = r['metrics']['claim_to_verify_latency_hours']
        # TASK first named 08:00, artifact ccc... 08:10; both independently verified 09:00 -> 1.0 h and 0.83 h
        self.assertEqual((latency['median'], latency['count'], latency['denominator']), (0.92, 2, 4))
        self.assertEqual(r['metrics']['artifact_reuse_count'], {'artifacts': 1, 'agents': 1})  # the King verified one
        self.assertEqual(r['interventions'], [])

    def test_missing_sources_are_reported_and_give_unknown(self):
        r = oc.run(self.coord, None, None, now=NOW)
        status = {s['kind']: s['status'] for s in r['sources']}
        self.assertEqual((status['artifacts'], status['events']), ('missing', 'missing'))
        self.assertEqual(r['metrics']['actions_per_100_messages']['reason'], 'events_not_available')
        self.assertTrue(any('artifacts is missing' in w for w in r['warnings']))

    def test_databases_are_opened_read_only(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.coord, self.arts)}
        self.report()
        conn = oc._connect(self.coord)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM tasks")
        finally:
            conn.close()
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.coord, self.arts)})

    def test_cli_writes_json_and_markdown(self):
        out = Path(self.tmp.name)
        oc.main(['--coordination', str(self.coord), '--artifacts', str(self.arts), '--events', str(self.events),
                 '--json', str(out / 'r.json'), '--md', str(out / 'r.md')])
        self.assertEqual(json.loads((out / 'r.json').read_text())['schema_version'], oc.SCHEMA)
        md = (out / 'r.md').read_text()
        self.assertIn('## Evidence coverage', md)
        self.assertIn('not proof of cause', md)


class ClassTests(unittest.TestCase):
    def test_ack_echo(self):
        self.assertEqual(oc.message_class('Acknowledged, will attend at 14:00.'), 'ack_echo')
        self.assertEqual(oc.message_class('Thanks. Could you send me the log path?'), 'unknown')

    def test_paraphrased_repeat(self):
        earlier = ['I will finalize the disk monitor provenance recipe and submit it for review.']
        self.assertEqual(oc.message_class("I'll finalize the disk monitor provenance recipe and submit it for your review.", earlier), 'repeat')

    def test_periodic_measurement_with_a_new_value_is_not_a_repeat(self):
        earlier = ['Disk usage on /mnt/hdd1 is 1% with 3481 GiB free (measured with df -BG).']
        self.assertEqual(oc.message_class('Disk usage on /mnt/hdd1 is 2% with 3470 GiB free (measured with df -BG).', earlier), 'progress')
        self.assertEqual(oc.message_class(earlier[0], earlier), 'repeat')

    def test_annotated_fixture_precision(self):
        labelled = json.loads((ROOT / 'tests/fixtures/outcomes_annotated.json').read_text())
        scores = oc.evaluate_classifier(labelled)
        for cls in oc.LOOP_CLASSES:
            self.assertGreaterEqual(scores[cls]['precision'], 0.9, cls)
        # Real progress is never counted as part of a loop.
        progress = [x for x in labelled if x['label'] == 'progress']
        self.assertFalse([x['text'] for x in progress if oc.message_class(x['text'], x['earlier']) in oc.LOOP_CLASSES])

    def test_loop_needs_four_messages_without_progress_or_outcome(self):
        base = '2026-10-12T0{}:00:00+00:00'
        texts = ['I will review it when you post it.', 'Thanks, I will finalize it and submit it shortly.',
                 'Will review it shortly.', 'Acknowledged.', 'Ran the check: exit code 0, 3 lines written to /tmp/x.log.']
        msgs = [msg(f'k{i}', ('01-king', '04-artisan')[i % 2], ('04-artisan', '01-king')[i % 2], t, base.format(i + 1))
                for i, t in enumerate(texts)]
        result = oc.loops(oc.analyse(msgs, now=NOW)['messages'])
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['length'], result[0]['message_ids'][-1]), (4, 'k3'))
        self.assertEqual(oc.loops(oc.analyse(msgs[:3], now=NOW)['messages']), [])


class InputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_paginated_har_messages_are_counted_once_and_headers_never_read(self):
        page = lambda ids: json.dumps({'total': 3, 'messages': [msg(i, '02-explorer', '07-methodologist', f'text {i}',
                                                                        '2026-10-12T08:00:00+00:00') for i in ids]})
        entry = lambda url, text: {'request': {'url': url, 'headers': [{'name': 'Cookie', 'value': 'session=SYNTHETIC-COOKIE-123'}]},
                                   'response': {'headers': [{'name': 'Set-Cookie', 'value': 'x=SYNTHETIC-COOKIE-123'}],
                                                'content': {'text': text}}}
        har = {'log': {'entries': [
            entry('http://village.test/api/conversations/messages?page=0', page(['a', 'b'])),
            entry('http://village.test/api/conversations/messages?page=1', page(['b', 'c'])),
            entry('http://village.test/api/conversations/messages?page=0', page(['a', 'b'])),
            entry('http://village.test/api/observatory', json.dumps({'messages': [{'id': 'zz', 'content': 'not a message'}]})),
        ]}}
        (self.dir / 'x.har').write_text(json.dumps(har))
        messages, stats = oc.load_exports([self.dir / 'x.har'])
        self.assertEqual(stats, {'read': 6, 'unique': 3, 'duplicates': 3})
        self.assertEqual(sorted(m['id'] for m in messages), ['a', 'b', 'c'])
        report = oc.run(exports=[self.dir / 'x.har'], now=NOW)
        self.assertEqual(report['metrics']['messages_sent'], 3)
        self.assertNotIn('SYNTHETIC-COOKIE', json.dumps(report))
        self.assertTrue(any('not the full corpus' in w for w in report['warnings']))

    def test_json_lines_export(self):
        (self.dir / 'x.jsonl').write_text('\n'.join(json.dumps(msg(i, 'a', 'b', 't', '2026-10-12T08:00:00Z')) for i in 'xyx'))
        self.assertEqual(oc.load_exports([self.dir / 'x.jsonl'])[1], {'read': 3, 'unique': 2, 'duplicates': 1})

    def test_time_zones_are_normalised_to_utc(self):
        msgs = [msg('late', 'a', 'b', 'one', '2026-10-12T10:30:00+02:00'),   # 08:30 UTC
                msg('early', 'b', 'a', 'two', '2026-10-12T08:15:00Z'),
                msg('naive', 'a', 'b', 'three', '2026-10-12T08:45:00')]
        analysis = oc.analyse(msgs, now=NOW)
        self.assertEqual([m['id'] for m in analysis['messages']], ['early', 'late', 'naive'])
        self.assertEqual(analysis['messages'][1]['timestamp'], '2026-10-12T08:30:00Z')
        report = oc.build_report(analysis)
        self.assertTrue(any('1 timestamps without a time zone' in w for w in report['warnings']))

    def test_window_filter(self):
        msgs = [msg('a', 'x', 'y', 't', '2026-10-11T23:00:00Z'), msg('b', 'x', 'y', 't', '2026-10-12T01:00:00Z')]
        since = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.assertEqual([m['id'] for m in oc.analyse(msgs, now=NOW, since=since)['messages']], ['b'])


class MeasurementTests(unittest.TestCase):
    def test_disk_claims_in_messages_are_unverified_and_not_comparable(self):
        msgs = [msg('d1', '07-methodologist', '02-explorer', 'For /mnt/hdd1 (450GB free) keep 80%.', '2026-10-10T23:24:00Z'),
                msg('d2', '07-methodologist', '02-explorer', '/mnt/hdd1: 18.6 GiB free of 20.0 GiB', '2026-10-11T01:00:00Z')]
        claims = oc.measurement_claims(msgs)
        # One value per mention of a mount point: the first size after it.
        self.assertEqual([(c['status'], c['value'], c['unit']) for c in claims],
                         [('unverified_claim', 450.0, 'GB'), ('unverified_claim', 18.6, 'GiB')])
        self.assertEqual(claims[0]['bytes'], 450 * 1000 ** 3)
        self.assertEqual({c['reason'] for c in oc.compare_measurements(claims)}, {'missing_provenance'})

    def test_only_the_same_mount_namespace_and_time_can_contradict(self):
        base = dict(host_id='n06-m10', device='/dev/sdb1', mountpoint='/mnt/hdd1', timestamp='2026-10-11T01:00:00Z')
        host = dict(base, id='h', mount_namespace='mnt:[4026531841]', bytes=3481 * 1024 ** 3)
        container = dict(base, id='c', mount_namespace='mnt:[4026532999]', bytes=int(18.6 * 1024 ** 3))
        later = dict(host, id='l', timestamp='2026-10-11T05:00:00Z', bytes=3000 * 1024 ** 3)
        same = dict(host, id='s', bytes=3000 * 1024 ** 3)
        by_pair = {(x['a'], x['b']): (x['status'], x['reason']) for x in oc.compare_measurements([host, container, later, same])}
        self.assertEqual(by_pair[('h', 'c')], ('not_comparable', 'different_source'))
        self.assertEqual(by_pair[('h', 'l')], ('not_comparable', 'different_time'))
        self.assertEqual(by_pair[('h', 's')], ('contradiction_candidate', None))


class NeutralityTests(unittest.TestCase):
    def test_nothing_in_the_village_imports_the_evaluation(self):
        # Research telemetry for the operator must never reach a resident's context, board or memory.
        for path in [*(ROOT / 'village').glob('*.py'), *(ROOT / 'web').glob('*.py'), *(ROOT / 'memory').glob('*.py')]:
            if path.name == 'outcomes.py':
                continue
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('outcomes import', text, path)
            self.assertNotIn('import outcomes', text, path)

    def test_not_part_of_the_runtime_release(self):
        self.assertNotIn('outcomes.py', (ROOT / 'village/release.py').read_text())
        self.assertNotIn('outcomes.py', (ROOT / 'scripts/install-runtime.py').read_text())


if __name__ == '__main__':
    unittest.main()
