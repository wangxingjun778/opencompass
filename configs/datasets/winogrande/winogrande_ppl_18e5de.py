from opencompass.openicl.icl_prompt_template import PromptTemplate
from opencompass.openicl.icl_retriever import ZeroRetriever
from opencompass.openicl.icl_inferencer import PPLInferencer
from opencompass.openicl.icl_evaluator import AccEvaluator
from opencompass.datasets import winograndeDataset

winogrande_reader_cfg = dict(
    input_columns=['opt1', 'opt2'],
    output_column='answer',
    train_split='validation',
    test_split='validation')

winogrande_infer_cfg = dict(
    prompt_template=dict(
        type=PromptTemplate,
        template={
            i: dict(round=[
                dict(role="HUMAN", prompt=f"Good sentence: {{opt{i+1}}}"),
            ])
            for i in range(2)
        }),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=PPLInferencer))

winogrande_eval_cfg = dict(evaluator=dict(type=AccEvaluator), )

winogrande_datasets = [
    dict(
        abbr='winogrande',
        type=winograndeDataset,
        path='winogrande',
        name='winogrande_xs',
        reader_cfg=winogrande_reader_cfg,
        infer_cfg=winogrande_infer_cfg,
        eval_cfg=winogrande_eval_cfg)
]