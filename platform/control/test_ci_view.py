import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from control.ci_view import project


class ProgressTest(unittest.TestCase):
    def test_failed_or_unobserved_steps_never_become_success_or_invented_timing(self):
        job={'id':'run','status':'DISPATCHED','created_at':100}
        events={'records':[{'phase':'L0','attributes':{},'outcome':'PASS','sequence':1,'occurred_at':'2026-10-01T00:00:00Z'},
                           {'phase':'L1','attributes':{},'outcome':'RUNNING','sequence':2,'occurred_at':'2026-10-01T00:00:01Z'}],'truncated':False}
        running=project(job,events)
        self.assertEqual((running['completed'],running['passed'],running['total']),(1,1,6))
        self.assertIsNone(running['steps'][0]['duration_s'])
        finished=project({**job,'status':'FAIL'},events,{'details':{'layers':[{'layer':'L0','outcome':'PASS'},{'layer':'L1','outcome':'FAIL'}]}})
        self.assertEqual((finished['completed'],finished['passed']),(2,1))
        self.assertEqual(finished['steps'][2]['status'],'NOT_RUN')
        self.assertNotEqual(finished['percent'],100)
        unknown=project({**job,'status':'UNKNOWN'},events)
        self.assertEqual(unknown['steps'][1]['status'],'UNKNOWN')


if __name__=='__main__': unittest.main()
