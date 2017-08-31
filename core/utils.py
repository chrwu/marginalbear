import math
import re
import opencc
import psycopg2
import logging
import datetime
from typing import Generator, List

URL_REGEX = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'


class OkLogger(object):

    def __init__(self, name, level=logging.INFO, fmt='[%(name)s] %(asctime)s %(levelname)s: %(message)s'):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        ch = logging.StreamHandler()
        chformatter = logging.Formatter(
            fmt,
            datefmt='[%d/%b/%Y %H:%M:%S]'
        )
        ch.setLevel(level)
        ch.setFormatter(chformatter)
        self.logger.addHandler(ch)


class QBag(object):

    def __init__(self, query):
        self.query = query


class Query(object):

    def __init__(self, stentence, ctype='text'):
        self.query = stentence
        self.ctype = ctype


class Word(dict):

    def __init__(self, word, pos='__unknown__'):
        dict.__init__(self, word=word, pos=pos)
        self.word = word
        self.pos = pos


class Vocab(Word):

    quality = 0.0
    tokenizer = 'jieba'
    postfreq = 1
    commentfreq = 1
    stopword = False
    weight = 1.0

    def __init__(self, word, pos='__unknown__', **kwargs):
        super().__init__(word, pos=pos)
        for k, v in kwargs.items():
            if self.__getattribute__(k) is not None:
                self.__setattr__(k, v)


class Post(object):

    post_id = -1
    publish_date = datetime.datetime.now()
    quality = 0.0
    similarity_score = 0.0
    retrieval_count = 0
    category = ''
    author = ''
    body = ''
    url = ''

    def __init__(self, vocabs, tokenizer_tag, **kwargs):
        self.vocabs = vocabs
        self.tokenizer_tag = tokenizer_tag
        for k, v in kwargs.items():
            if self.__getattribute__(k) is not None:
                self.__setattr__(k, v)


class Comment(object):

    post_id = -1
    quality = 0.0
    ctype = 'text'
    category = ''
    retrieval_count = 0
    floor = 1
    body = ''

    def __init__(self, vocabs, tokenizer_tag, **kwargs):
        self.vocabs = vocabs
        self.tokenizer_tag = tokenizer_tag
        for k, v in kwargs.items():
            if self.__getattribute__(k) is not None:
                self.__setattr__(k, v)


class LengthNotMatchException(Exception):
    pass


class PsqlAbstract(object):
    user = None
    db = None
    pw = None

    def __init__(self, username=None, db=None, password=None):
        if bool(username) and bool(db) and bool(password):
            PsqlAbstract.user = username
            PsqlAbstract.db = db
            PsqlAbstract.pw = password

    @staticmethod
    def session(keep=False):
        def _session(func):
            def _wrapper(self, *args, **kwargs):
                connect = psycopg2.connect(database=PsqlAbstract.db, user=PsqlAbstract.user, password=PsqlAbstract.pw)
                cursor = connect.cursor()
                ret = func(self, connect, cursor, **kwargs)
                if not keep:
                    PsqlAbstract._close(connect, cursor)

                return ret

            return _wrapper
        return _session

    @staticmethod
    def _close(connect, cursor):
        cursor.close()
        connect.close()

    def _execute(self, sql_string, data):
        connect = psycopg2.connect(database=PsqlAbstract.db, user=PsqlAbstract.user, password=PsqlAbstract.pw)
        cursor = connect.cursor()
        cursor.execute(sql_string, data)
        ret = cursor.fetchone()
        PsqlAbstract._close(connect, cursor)
        return ret

    @staticmethod
    def set_database_info(username, db, password):
        PsqlAbstract.user = username
        PsqlAbstract.db = db
        PsqlAbstract.pw = password


class PsqlQuery(PsqlAbstract):

    def __init__(self, username=None, db=None, password=None):
        super(self.__class__, self).__init__(username=username, db=db, password=password)
        self.schema = {}

    def upsert(self, q, data=None):
        return self._upsert(query_=q, data=data)

    @PsqlAbstract.session()
    def _upsert(self, connect, cursor, query_=None, data=None):
        cursor.execute(query_, data)
        connect.commit()
        return cursor.fetchall()

    def update(self, q, data=None):
        self._update(query_=q, data=data)

    @PsqlAbstract.session()
    def _update(self, connect, cursor, query_=None, data=None):
        cursor.execute(query_, data)
        connect.commit()

    def delete(self, q, data=None):
        self._delete(query_=q, data=data)

    @PsqlAbstract.session()
    def _delete(self, connect, cursor, query_=None, data=None):
        cursor.execute(query_, data)
        connect.commit()

    def query(self, q, data=None, skip=False):
        if not skip:
            self._get_schema(query_=q, data=data)
        return self._query(query_=q, data=data)

    @PsqlAbstract.session()
    def _get_schema(self, connect, cursor, query_=None, data=None):
        if query_ is None:
            return
        idx_semicln = query_.find(';')
        if idx_semicln > 0:
            query_ = query_[:idx_semicln]
        query_ += ' LIMIT 0;'
        cursor.execute(query_, data)
        schema = [desc[0] for desc in cursor.description]
        # print('Warning: schema changed:', schema)
        self.schema = {k: v for v, k in enumerate(schema)}

    @PsqlAbstract.session(keep=True)
    def _query(self, connect, cursor, query_=None, data=None):
        if query_ is None:
            return
        cursor.execute(query_, data)
        for record in cursor:
            yield record
        PsqlAbstract._close(connect, cursor)

    def query_all(self, sql_string, data=None):
        fetched = list(self.query(sql_string, data))
        schema = self.schema
        return fetched, schema


# tuple([('你', 'r', 'jieba'), ('好', 'a', 'jieba'), ('我', 'n', 'jieba')])
class PsqlQueryScript(object):

    query_netizen_sql = '''
        SELECT * FROM pttcorpus_netizen WHERE name IN %s;
    '''

    query_vocab_sql = '''
        SELECT * FROM pttcorpus_vocabulary WHERE (word, pos, tokenizer) IN %s;
    '''

    query_vocab_by_id = '''
        SELECT * FROM pttcorpus_vocabulary WHERE id IN %s;
    '''

    query_vocab2post_by_vid_sql = '''
        SELECT vocabulary_id, post_id FROM pttcorpus_vocabulary_post
        WHERE vocabulary_id IN %s;
    '''

    query_vocab2post_by_pid_sql = '''
        SELECT vocabulary_id, post_id FROM pttcorpus_vocabulary_post
        WHERE post_id IN %s;
    '''

    query_vocab2comment_by_vid_sql = '''
        SELECT vocabulary_id, comment_id FROM pttcorpus_vocabulary_comment
        WHERE vocabulary_id IN %s;
    '''

    query_vocab2comment_by_cmtid_sql = '''
        SELECT vocabulary_id, comment_id FROM pttcorpus_vocabulary_comment
        WHERE comment_id IN %s;
    '''

    query_post_by_id_sql = '''
        SELECT * FROM pttcorpus_post WHERE id IN %s;
    '''

    query_post_by_url_sql = '''
        SELECT * FROM pttcorpus_post WHERE url IN %s;
    '''

    query_comment_sql = '''
        SELECT * FROM pttcorpus_comment
        WHERE (post_id, tokenizer) IN %s;
    '''

    query_title_sql = '''
        SELECT * FROM pttcorpus_title
        WHERE (post_id, tokenizer) IN %s;
    '''

    query_post_by_vid_sql = '''
        SELECT post_id FROM pttcorpus_vocabulary_post
        WHERE vocabulary_id IN %s;
    '''

    query_title_vocab_ids_by_post_id_sql = '''
        SELECT post_id,
               string_agg(vocabulary_id::character varying, ',') AS vocab_ids
        FROM pttcorpus_vocabulary_post
        WHERE post_id IN %s
        GROUP BY post_id;
    '''

    query_comment_vocab_ids_by_post_id_sql = '''
        SELECT pttcorpus_comment.post_id,
               string_agg(pttcorpus_vocabulary_comment.vocabulary_id::character varying, ',') AS vocab_ids
        FROM pttcorpus_comment
        JOIN pttcorpus_vocabulary_comment ON
        pttcorpus_vocabulary_comment.comment_id = pttcorpus_comment.id
        WHERE pttcorpus_comment.post_id IN %s
        GROUP BY pttcorpus_comment.post_id;
    '''


# summation(tf * (k1 + 1) /(tf + k1*(1 - b + b*len(doc)/AVE_DOC_LEN)))
# k1 = [1.2, 2.0]
def bm25_similarity(vocab, doc, k1=1.5, b=0.75):
    doc_num = 300000.0
    ave_title_len = 19.0
    doc_len = len(doc)

    def _bm25(v):
        if v['word'] in doc:
            idf = math.log((doc_num - max(1.0, v['docfreq'])) / max(1.0, v['docfreq']))
            tf = v['termweight']
            return idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / ave_title_len))
        else:
            return 0.0

    score = sum([_bm25(v) for v in vocab])
    return score


def pos_jaccard_similarity(vocab, doc):
    doc_num = 300000
    invocab = []
    for v in vocab:
        if v['word'] in doc and v not in invocab:
            invocab.append(v)

    tfidf = [v['termweight'] * math.log(doc_num / min(1.0, v['docfreq'])) for v in invocab]
    union = set([v['word'] for v in vocab] + doc)
    score = sum(tfidf) / float(len(union))
    return score


def clean_comment(comment_string):
    union_comment = {}
    cleaned_comment = []

    # anony_num = 0
    for line, mix in enumerate(comment_string.split('\n'), 1):
        idx = mix.find(':')
        if idx < 0:
            # anony_num += 1
            audience = 'anonymous'
            comment = mix.strip()
            # comment, ctype = clean_query(mix.strip())

            # union_comment[name] = {}
            # union_comment[name]['comment'] = [{'content': mix.strip(), 'line': line}]
        else:
            audience, comment = mix[:idx].strip(), mix[idx + 1:].strip()
            # comment, ctype = clean_query(raw)

        if audience in union_comment:
            union_comment[audience]['comment'].append({
                'content': comment,
                'line': line,
                # 'ctype': ctype
            })
        else:
            union_comment[audience] = {}
            union_comment[audience]['comment'] = [{
                'content': comment,
                'line': line,
                # 'ctype': ctype
            }]

    for key, allcomment in union_comment.items():
        appendcomment = []
        line = -10
        for cmt in allcomment['comment']:

            if (cmt['line'] - line) < 2:
                appendcomment[-1]['comment'] += cmt['content']

            else:
                appendcomment.append({
                    'comment': cmt['content'],
                    'audience': key,
                    'floor': cmt['line'],
                    # 'ctype': cmt['ctype']
                })

            line = cmt['line']

        cleaned_comment.extend(appendcomment)

    return cleaned_comment


def jaccard_similarity(vocab, doc):
    wlist = [v['word'] for v in vocab]
    wset = set(wlist)
    dset = set(doc)
    union = set(wlist + doc)
    score = len(wset.intersection(dset)) / float(len(union))
    return score


def contain_url(query):
    matched = re.search(URL_REGEX, query)

    if bool(matched):
        return True, matched.group()
    else:
        return False, None


def query2lower(query: str) -> str:
    """Convert the query string to lowercase."""
    return query.lower()


def to_lower(query: str) -> str:
    """Convert the query string to lowercase."""
    return query.lower()


def to_halfwidth(query: str) -> str:
    """Convert the query string to halfwidth."""
    """
    全形字符 unicode 編碼從 65281 ~ 65374(十六進制 0xFF01 ~ 0xFF5E)
    半形字符 unicode 編碼從 33 ~ 126(十六進制 0x21~ 0x7E)
    空格比較特殊, 全形為12288(0x3000), 半形為32(0x20)
    而且除空格外, 全形/半形按 unicode 編碼排序在順序上是對應的
    所以可以直接通過用+-法來處理非空格字元, 對空格單獨處理.
    """
    rstring = ""
    for char in query:
        code = ord(char)
        if code == 0x3000:
            code = 0x0020
        else:
            code -= 0xfee0
        if code < 0x0020 or code > 0x7e:  # fallback check
            rstring += char
        else:
            rstring += chr(code)
    return rstring


def query2halfwidth(query: str) -> str:
    """Convert the query string to halfwidth."""
    """
        全形字符 unicode 編碼從 65281 ~ 65374(十六進制 0xFF01 ~ 0xFF5E)
        半形字符 unicode 編碼從 33 ~ 126(十六進制 0x21~ 0x7E)
        空格比較特殊, 全形為12288(0x3000), 半形為32(0x20)
        而且除空格外, 全形/半形按 unicode 編碼排序在順序上是對應的
        所以可以直接通過用+-法來處理非空格字元, 對空格單獨處理.
    """

    rstring = ""
    for char in query:
        code = ord(char)
        if code == 0x3000:
            code = 0x0020
        else:
            code -= 0xfee0
        if code < 0x0020 or code > 0x7e:  # fallback check
            rstring += char
        else:
            rstring += chr(code)
    return rstring


def clean_query(query):
    contain, url = contain_url(query)
    if contain:
        return url, 'url'
    else:
        return query2halfwidth(query).lower(), 'text'
