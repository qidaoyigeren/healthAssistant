"""Check the design DDL and a synthetic bitemporal example, not a v4 runtime."""
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
db = sqlite3.connect(":memory:")
db.executescript((HERE / "schema_v4_draft.sql").read_text("utf-8"))
db.execute("INSERT INTO subjects VALUES ('mother','synthetic',NULL)")
for seq, known in ((1, 820), (2, 905)):
    db.execute("INSERT INTO commits VALUES (?,?,?,'draft')", (seq, known, known))
    db.execute("""INSERT INTO source_events(event_id,subject_id,client_instance_id,client_event_id,
        session_id,turn_id,event_type,source_type,source_actor,independent_source_group,
        received_us,timezone,time_anchor_us,process_status,committed_seq)
        VALUES (?,'mother','test',?,'s',?,'report','caregiver_report','synthetic','synthetic',?,'UTC',?,'committed',?)""",
        (f'e{seq}', f'c{seq}', f't{seq}', known, known, seq))
    ref = f'memory:assertion:a{seq}@v1'
    db.execute("INSERT INTO memory_objects VALUES (?,'mother',?,1,'assertion',?,NULL)", (ref, f'a{seq}', seq))
    db.execute("""INSERT INTO assertions(ref,subject_id,event_id,candidate_index,subject_relation,namespace,
        canonical_key,assertion_mode,scope_json,span_kind,extraction_reliability,reliability_reasons_json,
        source_verification,time_precision,time_basis)
        VALUES (?,'mother',?,0,'target','medication','regimen-a','affirmed','{}','payload_pointer',
        'rule_supported','[]','unverified','day','synthetic')""", (ref, f'e{seq}'))
    db.execute("""INSERT INTO mutations(mutation_id,subject_id,event_id,commit_seq,operation,new_ref,actor,reason_code)
        VALUES (?,'mother',?,?,'accept_report',?,'synthetic','fixture')""", (f'm{seq}', f'e{seq}', seq, ref))
for sid, start, end, sysstart, sysend, ref, status, cause in (
    ('s1', 820, None, 1, 2, 'memory:assertion:a1@v1', 'active', 'm1'),
    ('s2', 820, 901, 2, None, 'memory:assertion:a1@v1', 'active', 'm2'),
    ('s3', 901, None, 2, None, 'memory:assertion:a2@v1', 'stopped', 'm2'),
):
    db.execute("""INSERT INTO state_slices VALUES (?,'mother','medication','regimen-a','reported',?,
        ?,'recorded_as_reported',?,?,'day',?,?,?)""",
        (sid, ref, json.dumps({'medication_state': status}), start, end, sysstart, sysend, cause))
checks = []
for valid, known, expected in ((903, 903, 's1'), (903, 905, 's3'), (825, 905, 's2'), (819, 905, None)):
    k = db.execute("SELECT COALESCE(MAX(seq),0) FROM commits WHERE known_us<=?", (known,)).fetchone()[0]
    actual = db.execute("""SELECT slice_id FROM state_slices
        WHERE subject_id='mother' AND sys_from_seq<=? AND (sys_to_seq IS NULL OR ?<sys_to_seq)
        AND valid_from_us<=? AND (valid_to_us IS NULL OR ?<valid_to_us)""", (k, k, valid, valid)).fetchall()
    assert actual == ([] if expected is None else [(expected,)]), (valid, known, actual)
    checks.append({'valid': valid, 'known': known, 'expected_slice': expected, 'passed': True})
assert db.execute('PRAGMA foreign_key_check').fetchall() == []
assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
try:
    db.execute("INSERT INTO dependencies VALUES ('other','memory:assertion:a1@v1','memory:assertion:a2@v1','fact','{}')")
except sqlite3.IntegrityError:
    cross_subject_rejected = True
else:
    raise AssertionError('cross-subject dependency accepted')
result = {
    'schema_table_count': db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0],
    'ddl_executed': True, 'foreign_key_check': 'passed', 'integrity_check': 'ok',
    'cross_subject_dependency_rejected': cross_subject_rejected, 'time_matrix': checks,
    'limitation': 'Fixture integers encode dates for illustration, not production timestamps. Checks validate DDL and temporal predicates only; no migration, policy guard, projection builder, task runner or v4 application implemented.'
}
db.close()
(HERE / 'draft_check_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
