import datetime
from unittest import TestCase
from uuid import NAMESPACE_URL

from eventsourcing.infrastructure.transcoding import ObjectJSONEncoder, ObjectJSONDecoder
from eventsourcing.utils.time import utc_timezone


class TestObjectJSONEncoder(TestCase):
    def test_default(self):
        encoder = ObjectJSONEncoder()
        expect = '1'
        self.assertEqual(encoder.encode(1), expect)

        value = datetime.datetime(2011, 1, 1, 1, 1, 1)
        expect = '{"ISO8601_datetime": "2011-01-01T01:01:01.000000"}'
        self.assertEqual(encoder.encode(value), expect)

        value = datetime.datetime(2011, 1, 1, 1, 1, 1, tzinfo=utc_timezone)
        expect = '{"ISO8601_datetime": "2011-01-01T01:01:01.000000+0000"}'
        self.assertEqual(encoder.encode(value), expect)

        value = datetime.date(2011, 1, 1)
        expect = '{"ISO8601_date": "2011-01-01"}'
        self.assertEqual(encoder.encode(value), expect)

        value = NAMESPACE_URL
        expect = '{"UUID": "6ba7b8119dad11d180b400c04fd430c8"}'
        self.assertEqual(encoder.encode(value), expect)

        value = Object(NAMESPACE_URL)
        expect = ('{"__class__": {"topic": "test_transcoding#Object", "state": {"a": {"UUID": '
                  '"6ba7b8119dad11d180b400c04fd430c8"}}}}')
        self.assertEqual(encoder.encode(value), expect)


class TestObjectJSONDecoder(TestCase):
    def test_default(self):
        decoder = ObjectJSONDecoder()
        self.assertEqual(decoder.decode('1'), 1)

        value = '{"ISO8601_datetime": "2011-01-01T01:01:01.000000"}'
        expect = datetime.datetime(2011, 1, 1, 1, 1, 1)
        self.assertEqual(decoder.decode(value), expect)

        value = '{"ISO8601_datetime": "2011-01-01T01:01:01.000000+0000"}'
        expect = datetime.datetime(2011, 1, 1, 1, 1, 1, tzinfo=utc_timezone)
        self.assertEqual(decoder.decode(value), expect)

        value = '{"ISO8601_date": "2011-01-01"}'
        expect = datetime.date(2011, 1, 1)
        self.assertEqual(decoder.decode(value), expect)

        value = '{"UUID": "6ba7b8119dad11d180b400c04fd430c8"}'
        expect = NAMESPACE_URL
        self.assertEqual(decoder.decode(value), expect)

        value = ('{"__class__": {"topic": "test_transcoding#Object", "state": {"a": {"UUID": '
                 '"6ba7b8119dad11d180b400c04fd430c8"}}}}')
        expect = Object(NAMESPACE_URL)
        self.assertEqual(decoder.decode(value), expect)


class Object(object):
    def __init__(self, a):
        self.a = a

    def __eq__(self, other):
        return self.a == other.a

