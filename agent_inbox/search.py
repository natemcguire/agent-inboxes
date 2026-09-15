"""Ranked, read-only conversation retrieval shared by local and hosted inboxes."""
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

from agent_inbox.models import ValidationError

SCHEMA = '''
CREATE VIRTUAL TABLE IF NOT EXISTS mail_search USING fts5(
    subject, body_markdown, content='emails', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3 4'
);
CREATE VIRTUAL TABLE IF NOT EXISTS mail_search_words USING fts5vocab(mail_search, 'row');
CREATE TRIGGER IF NOT EXISTS mail_search_insert AFTER INSERT ON emails BEGIN
    INSERT INTO mail_search(rowid,subject,body_markdown) VALUES(new.rowid,new.subject,new.body_markdown);
END;
CREATE TRIGGER IF NOT EXISTS mail_search_delete AFTER DELETE ON emails BEGIN
    INSERT INTO mail_search(mail_search,rowid,subject,body_markdown) VALUES('delete',old.rowid,old.subject,old.body_markdown);
END;
CREATE TRIGGER IF NOT EXISTS mail_search_update AFTER UPDATE OF subject,body_markdown ON emails BEGIN
    INSERT INTO mail_search(mail_search,rowid,subject,body_markdown) VALUES('delete',old.rowid,old.subject,old.body_markdown);
    INSERT INTO mail_search(rowid,subject,body_markdown) VALUES(new.rowid,new.subject,new.body_markdown);
END;
'''


def initialize(conn):
    # This runs under the same transaction as the backfill. New/replayed mail is
    # maintained by triggers, including deletes, imports and subject corrections.
    conn.execute('BEGIN IMMEDIATE')
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mail_search'").fetchone()
        # executescript commits implicitly in sqlite3: execute each complete
        # statement ourselves so schema + index population stay atomic.
        import sqlite3
        statement = ''
        for char in SCHEMA:
            statement += char
            if char == ';' and sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        if not exists:
            conn.execute("INSERT INTO mail_search(mail_search) VALUES('rebuild')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


def parts(text):
    """Return text and highlight spans, never server-generated HTML."""
    out = []
    for index, text in enumerate(re.split('[\x01\x02]', text or '')):
        if text:
            out.append({'text': text, 'match': bool(index % 2)})
    return out


def excerpt_parts(text):
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|```[^\n]*$)', '', text or '')
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text, flags=re.S)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    return parts(re.sub(r'\s+', ' ', text).strip())


def parse(query):
    filters = {}
    def extract(match):
        key, value = match.group(1).lower(), match.group(2).strip('"')
        if value:
            filters[key] = value.lower()
        return ' '
    text = re.sub(r'\b(project|from|after|before):("[^"]*"|[^\s]+)', extract, query, flags=re.I)
    groups = []
    for match in re.finditer(r'"([^"]+)"|([^\s"]+)', text):
        words = re.findall(r'[^\W_]+', normalized(match.group(1) or match.group(2)), re.UNICODE)
        if words:
            groups.append((words, match.group(1) is not None))
    if sum(len(words) for words, _ in groups) > 16:
        raise ValidationError('invalid_search', 'Use up to 16 search words')
    for key in ('after', 'before'):
        if key in filters:
            try:
                if date.fromisoformat(filters[key]).isoformat() != filters[key]:
                    raise ValueError()
            except ValueError:
                raise ValidationError('invalid_search', f'{key}: needs a date like 2026-09-15')
    return groups, filters


def expression(groups):
    # Never pass user FTS syntax through: operators, quotes and punctuation are
    # parsed as ordinary words. Quoted phrases remain exact; words autocomplete.
    return ' AND '.join('"' + ' '.join(words) + '"' if phrase else
                        ' AND '.join('"' + word + '"*' for word in words)
                        for words, phrase in groups)


class ConversationSearch:
    def __init__(self, conn):
        self.conn = conn

    def search(self, query='', project='', inbox='', limit=12, offset=0):
        if not isinstance(query, str) or len(query) > 500:
            raise ValidationError('invalid_search', 'Search must be at most 500 characters')
        if not 1 <= limit <= 30 or not 0 <= offset <= 1000:
            raise ValidationError('invalid_search', 'Invalid search page')
        query = query.strip()
        groups, filters = parse(query)
        where, params = [], []
        for slug in [project, filters.get('project')]:
            if slug:
                where.append('''EXISTS (SELECT 1 FROM thread_inboxes ti JOIN inboxes i ON i.id=ti.inbox_id
                    JOIN projects p ON p.id=i.project_id WHERE ti.thread_id=t.id AND p.slug=?)''')
                params.append(slug.lower())
        if inbox:
            where.append('''EXISTS (SELECT 1 FROM thread_inboxes ti JOIN inboxes i ON i.id=ti.inbox_id
                JOIN projects p ON p.id=i.project_id WHERE ti.thread_id=t.id AND i.local_part||'@'||p.slug=?)''')
            params.append(inbox.lower())
        if filters.get('from'):
            field = "sender.local_part||'@'||sp.slug" if '@' in filters['from'] else 'sender.local_part'
            where.append(field + '=? COLLATE NOCASE')
            params.append(filters['from'])
        for key, operator in [('after', '>='), ('before', '<')]:
            if key in filters:
                where.append('e.sent_at' + operator + '?')
                params.append(filters[key])
        scope = ' AND '.join(where) or '1'
        target_project = project or filters.get('project') or (inbox.split('@')[-1] if inbox else '')

        def find(match, page_offset=offset, page_limit=limit):
            if match:
                table = 'mail_search JOIN emails e ON e.rowid=mail_search.rowid'
                match_where = 'mail_search MATCH ? AND '
                arguments = [match] + params
                ranking = 'bm25(mail_search,8.0,1.0)'
                title = "highlight(mail_search,0,char(1),char(2))"
                snippet = "snippet(mail_search,1,char(1),char(2),' … ',36)"
            else:
                table, match_where, arguments = 'emails e', '', params
                ranking, title, snippet = '0.0', 'e.subject', 'substr(e.body_markdown,1,240)'
            return self.conn.execute(f'''
                WITH hits AS MATERIALIZED (
                    SELECT e.id AS email_id,e.thread_id,e.sent_at,e.subject,{ranking} AS score,
                        {title} AS marked_title,{snippet} AS excerpt,
                        sender.local_part||'@'||sp.slug AS sender
                    FROM {table} JOIN threads t ON t.id=e.thread_id
                    JOIN inboxes sender ON sender.id=e.from_inbox_id JOIN projects sp ON sp.id=sender.project_id
                    WHERE {match_where}{scope}
                ), grouped AS (
                    SELECT *,ROW_NUMBER() OVER (PARTITION BY thread_id
                        ORDER BY CASE WHEN instr(excerpt,char(1))>0 THEN 0 ELSE 1 END,score,sent_at DESC,email_id) AS position,
                        COUNT(*) OVER (PARTITION BY thread_id) AS match_count FROM hits
                )
                SELECT *,COUNT(*) OVER () AS total FROM grouped WHERE position=1
                ORDER BY score,sent_at DESC,thread_id LIMIT ? OFFSET ?
            ''', arguments + [page_limit, page_offset]).fetchall()

        match = expression(groups)
        rows = find(match)
        correction = None
        if not rows and match:
            # Bounded typo recovery. Candidate words are only disclosed after
            # the corrected query produces results inside the authorized scope.
            corrected = []
            changes = 0
            for words, phrase in groups:
                fixed = list(words)
                if not phrase:
                    for index, word in enumerate(words):
                        if len(word) < 4 or changes >= 2:
                            continue
                        exact = self.conn.execute('SELECT 1 FROM mail_search_words WHERE term>=? AND term<? LIMIT 1',
                                                  (word, word + '\uffff')).fetchone()
                        if exact:
                            continue
                        candidates = self.conn.execute('''SELECT term,doc FROM mail_search_words
                            WHERE term>=? AND term<? AND length(term) BETWEEN ? AND ?
                            ORDER BY doc DESC LIMIT 600''', (word[0], word[0]+'\uffff', len(word)-2, len(word)+2)).fetchall()
                        ranked = sorted(((SequenceMatcher(None, word, r['term']).ratio(), r['doc'], r['term']) for r in candidates), reverse=True)
                        if ranked and ranked[0][0] >= .74:
                            fixed[index] = ranked[0][2]
                            changes += 1
                corrected.append((fixed, phrase))
            if changes:
                corrected_rows = find(expression(corrected))
                if corrected_rows:
                    rows = corrected_rows
                    correction = ' '.join(('"'+' '.join(words)+'"') if phrase else ' '.join(words) for words, phrase in corrected)
                    correction += ''.join(f' {key}:{value}' for key, value in filters.items())

        results = []
        for row in rows:
            projects = [r[0] for r in self.conn.execute('''SELECT DISTINCT p.slug FROM thread_inboxes ti
                JOIN inboxes i ON i.id=ti.inbox_id JOIN projects p ON p.id=i.project_id
                WHERE ti.thread_id=? ORDER BY p.slug''', (row['thread_id'],))]
            count = self.conn.execute('SELECT COUNT(*) FROM emails WHERE thread_id=?', (row['thread_id'],)).fetchone()[0]
            results.append({key: row[key] for key in ('thread_id','email_id','subject','sender','sent_at','match_count')} |
                {'project': target_project or (projects[0] if projects else ''), 'projects': projects,
                 'message_count': count, 'title': parts(row['marked_title']), 'excerpt': excerpt_parts(row['excerpt'])})
        total = rows[0]['total'] if rows else 0
        return {'query': query, 'correction': correction, 'filters': filters, 'results': results, 'total': total,
                'next_offset': offset + len(rows) if offset + len(rows) < total else None}
