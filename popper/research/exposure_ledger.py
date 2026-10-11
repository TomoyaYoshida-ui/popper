"""Cross-study exposure identities with cohort-scoped, one-way confirmation."""
import json
import sqlite3
from pathlib import Path
from ..core import ProtocolError, canonical, digest


class ExposureLedger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS samples(identity TEXT PRIMARY KEY,batch TEXT,task TEXT,split TEXT);
            CREATE TABLE IF NOT EXISTS cohorts(batch TEXT,task TEXT,registration TEXT,roster TEXT,
                PRIMARY KEY(batch,task));
            CREATE TABLE IF NOT EXISTS analysis_units(identity TEXT PRIMARY KEY,batch TEXT,task TEXT,split TEXT);
            CREATE TABLE IF NOT EXISTS unit_cohorts(batch TEXT,task TEXT,registration TEXT,PRIMARY KEY(batch,task));
        ''')

    def close(self):
        self.conn.close()

    def reserve(self, batch, task, splits, *, groups=None):
        rows = []
        by_identity = {}
        for split, records in splits.items():
            if split not in {'train', 'dev', 'confirmation'}:
                raise ProtocolError('Unknown data role')
            for row in records:
                identities = {digest({'features': row['features']}), row.get('sample_identity')}
                for identity in identities - {None}:
                    if identity in by_identity and by_identity[identity] != split:
                        raise ProtocolError('Identical sample or input group crosses splits')
                    by_identity[identity] = split
                    rows.append((identity, batch, task, split))
        registration = digest(sorted(set(rows)))
        units={}
        if groups is not None:
            if set(groups)!={row['id'] for records in splits.values() for row in records}:
                raise ProtocolError('Exposure groups must cover all frozen samples')
            for split,records in splits.items():
                for row in records:
                    identity=digest({'analysis_unit':groups[row['id']]})
                    if identity in units and units[identity]!=split:
                        raise ProtocolError('Analysis unit crosses exposure splits')
                    units[identity]=split
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            old = self.conn.execute('SELECT registration FROM cohorts WHERE batch=? AND task=?', (batch, task)).fetchone()
            if old:
                if old[0] != registration:
                    raise ProtocolError('Exposure cohort registration changed')
            else:
                for identity, *_ in rows:
                    if self.conn.execute('SELECT 1 FROM samples WHERE identity=?', (identity,)).fetchone():
                        raise ProtocolError('New task overlaps previously reserved or exposed data')
                self.conn.executemany('INSERT OR IGNORE INTO samples VALUES(?,?,?,?)', rows)
                self.conn.execute('INSERT INTO cohorts VALUES(?,?,?,NULL)', (batch, task, registration))
            if groups is not None:
                unit_registration=digest(sorted(units.items()))
                prior=self.conn.execute('SELECT registration FROM unit_cohorts WHERE batch=? AND task=?',(batch,task)).fetchone()
                if prior and prior[0]!=unit_registration:
                    raise ProtocolError('Exposure analysis-unit registration changed')
                if not prior:
                    for identity in units:
                        if self.conn.execute('SELECT 1 FROM analysis_units WHERE identity=?',(identity,)).fetchone():
                            raise ProtocolError('Analysis unit overlaps previously reserved data')
                    self.conn.executemany('INSERT INTO analysis_units VALUES(?,?,?,?)',
                        [(identity,batch,task,split) for identity,split in units.items()])
                    self.conn.execute('INSERT INTO unit_cohorts VALUES(?,?,?)',(batch,task,unit_registration))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise
        return registration

    def open_confirmation(self, batch, task, frozen_roster):
        identity = digest(frozen_roster)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('SELECT roster FROM cohorts WHERE batch=? AND task=?', (batch, task)).fetchone()
            if row is None or (row[0] is not None and row[0] != identity):
                raise ProtocolError('Confirmation cohort is missing or already bound to another roster')
            self.conn.execute('UPDATE cohorts SET roster=? WHERE batch=? AND task=?', (identity, batch, task))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise
        return identity
