import unittest
from performance import ReadCache

class CacheTests(unittest.TestCase):
    def test_expiry(self):
        clock = [0]
        c = ReadCache(clock=lambda: clock[0])
        g, data = c.lookup('a'); self.assertIsNone(data)
        c.store('a', b'one', 20, g)
        self.assertEqual(c.lookup('a')[1], b'one')
        clock[0] = 20
        self.assertIsNone(c.lookup('a')[1]); self.assertEqual(c.size, 0)
    def test_invalidation_and_inflight(self):
        c = ReadCache(); g, _ = c.lookup('a')
        c.store('a', b'old', 20, g); c.clear()
        c.store('a', b'stale', 20, g)
        self.assertIsNone(c.lookup('a')[1])
    def test_bounds_and_isolation(self):
        c = ReadCache(limit=2, max_bytes=5)
        for k in ('u1','u2','u3'):
            g, _ = c.lookup(k); c.store(k,b'12',20,g)
        self.assertIsNone(c.lookup('u1')[1]); self.assertEqual(c.size,4)
        self.assertEqual(c.lookup('u2')[1],b'12')
        g,_=c.lookup('large'); c.store('large',b'123456',20,g)
        self.assertIsNone(c.lookup('large')[1])
    def test_replacement(self):
        c=ReadCache(); g,_=c.lookup('k'); c.store('k',b'123',20,g); c.store('k',b'12',20,g)
        self.assertEqual(c.size,2)

if __name__ == '__main__': unittest.main()
