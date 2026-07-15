# 에너지 사용량 이상탐지 모델 고도화 제언 (v5.1 기반)

## 1. 개요

현재의 `modeling_v5.1.py` 모델은 **점 예측(Point Forecasting)** 방식의 앙상블 모델로 구현되어 있습니다. 이상탐지 시스템으로 전환하기 위해서는 '정답'을 맞히는 것에서 나아가, **'정상 범위(Confidence Interval)'**를 계산하는 로직이 핵심입니다.

## 2. 핵심 고도화 전략: 분위수 회귀 (Quantile Regression)

### 2.1 개념

일반적인 회귀 모델이 평균값(P50)을 예측한다면, 분위수 회귀는 데이터의 하위 5%(P5)와 상위 95%(P95) 지점을 함께 예측합니다.

- **정상 구간:** [P5 예측값, P95 예측값] 사이의 영역
- **이상 신호:** 실제 사용량이 P95를 초과하거나 P5 미만으로 떨어지는 경우

### 2.2 코드 수정 가이드

`modeling_v5.1.py`의 `train_models` 함수를 아래와 같이 확장하여 분위수 모델을 학습할 수 있습니다.

```python
# train_models 함수 내 LightGBM Quantile 설정 예시
def train_quantile_models(X_tr, y_tr, alpha):
    """
    alpha=0.05 (하한선), 0.50 (중앙값), 0.95 (상한선)를 각각 학습
    """
    model = LGBMRegressor(
        objective="quantile",
        alpha=alpha,
        n_estimators=3000,
        learning_rate=0.05,
        random_state=42
    )
    model.fit(X_tr, y_tr)
    return model
```
