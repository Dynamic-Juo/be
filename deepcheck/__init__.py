"""DeepCheck — 영상 조작 가능성 분석 파이프라인.

서브모듈을 여기서 미리 import하지 않는다. 예전에는 패키지를 import하는 것만으로
torch·transformers까지 전부 로드돼서, 설정 하나 읽으려 해도 무거운 의존성이
따라왔다. 필요한 모듈을 직접 import한다 (`from deepcheck import pipeline` 등).
"""

__version__ = "0.2.0"
