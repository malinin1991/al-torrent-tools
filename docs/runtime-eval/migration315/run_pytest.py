import os
import sys
expected=os.environ['ALTT_EVAL_EXPECT_GIL']=='1'
before=sys._is_gil_enabled()
assert before==expected,(before,expected)
import pytest
assert sys._is_gil_enabled()==expected, "pytest import changed GIL"
exit_code=pytest.main(['-q','-p','no:cacheprovider'])
after=sys._is_gil_enabled()
print(f'EVALUATED_GIL_STATE before={before} after={after} expected={expected}',flush=True)
assert after==expected,(after,expected)
raise SystemExit(exit_code)
