# Copyright 2022 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.dsl.input_resolution.canned_resolver_functions."""

from typing import Dict, Sequence

import tensorflow as tf

from tfx import types
from tfx.dsl.compiler import compiler_context
from tfx.dsl.compiler import node_inputs_compiler
from tfx.dsl.components.base import base_component
from tfx.dsl.components.base import base_driver
from tfx.dsl.components.base import base_executor
from tfx.dsl.components.base import executor_spec
from tfx.dsl.control_flow import for_each
from tfx.dsl.input_resolution import canned_resolver_functions
from tfx.dsl.input_resolution import resolver_op
from tfx.dsl.input_resolution.ops import test_utils
from tfx.orchestration import pipeline
from tfx.orchestration.portable import inputs_utils
from tfx.proto.orchestration import pipeline_pb2
from tfx.types import artifact
from tfx.types import channel as channel_types
from tfx.types import channel_utils
from tfx.types import component_spec
from tfx.types import resolved_channel

from ml_metadata.proto import metadata_store_pb2


class _FakeSpec(component_spec.ComponentSpec):
  """_FakeComponent component spec."""

  PARAMETERS = {}
  INPUTS = {
      'x': component_spec.ChannelParameter(
          type=artifact.Artifact, optional=True
      ),
      'model': component_spec.ChannelParameter(
          type=artifact.Artifact, optional=True
      ),
      'examples': component_spec.ChannelParameter(
          type=artifact.Artifact, optional=True
      ),
  }
  OUTPUTS = {}


class _FakeComponent(base_component.BaseComponent):
  SPEC_CLASS = _FakeSpec

  EXECUTOR_SPEC = executor_spec.ExecutorClassSpec(base_executor.EmptyExecutor)

  DRIVER_CLASS = base_driver.BaseDriver

  def __init__(self, id: str, inputs=None, exec_properties=None):  # pylint: disable=redefined-builtin
    super().__init__(spec=_FakeSpec())
    self.with_id(id)

    # We override the inputs, exec_properties, and outputs.
    self._inputs = inputs or {}
    self._exec_properties = exec_properties or {}
    self._outputs = {}

  def output(self, key: str, artifact_type=test_utils.DummyArtifact):
    if key not in self._outputs:
      self._outputs[key] = channel_types.OutputChannel(artifact_type, self, key)
    return self._outputs[key]

  @property
  def inputs(self) ->...:
    return self._inputs

  @property
  def exec_properties(self) ->...:
    return self._exec_properties

  @property
  def outputs(self) ->...:
    return self._outputs


def _compile_inputs(
    inputs: Dict[str, channel_types.BaseChannel]) -> pipeline_pb2.PipelineNode:
  """Returns a compiled PipelineNode from the _FakeComponent inputs dict."""
  node = _FakeComponent('_FakeComponent', inputs=inputs)
  p = pipeline.Pipeline(pipeline_name='pipeline', components=[node])
  ctx = compiler_context.PipelineContext(p)
  node_inputs = pipeline_pb2.NodeInputs()

  # Compile the NodeInputs and wrap in a PipelineNode.
  node_inputs_compiler.compile_node_inputs(ctx, node, node_inputs)
  return pipeline_pb2.PipelineNode(inputs=node_inputs)


class CannedResolverFunctionsTest(
    test_utils.ResolverTestCase,
):

  def setUp(self):
    super().setUp()
    self.init_mlmd()
    self.enter_context(self.mlmd_handle)

  def assertResolvedAndMLMDArtifactEqual(
      self,
      resolved_artifact: metadata_store_pb2.Artifact,
      mlmd_artifact: metadata_store_pb2.Artifact,
      check_span_and_version: bool = False,
  ):
    """Checks that a MLMD artifacts and resolved artifact are equal."""
    self.assertEqual(mlmd_artifact.id, resolved_artifact.id)
    self.assertEqual(mlmd_artifact.type_id, resolved_artifact.type_id)
    self.assertEqual(mlmd_artifact.uri, resolved_artifact.uri)
    self.assertEqual(mlmd_artifact.state, resolved_artifact.state)

    if check_span_and_version:
      self.assertEqual(mlmd_artifact.properties['span'],
                       resolved_artifact.properties['span'])
      self.assertEqual(mlmd_artifact.properties['version'],
                       resolved_artifact.properties['version'])

  def _insert_artifacts_into_mlmd(
      self, spans: Sequence[int],
      versions: Sequence[int]) -> Sequence[metadata_store_pb2.Artifact]:
    """Inserts artifacts with the given spans and versions into MLMD."""
    mlmd_context = self.put_context('pipeline', 'pipeline')

    mlmd_artifacts = []
    for span, version in zip(spans, versions):
      mlmd_artifacts.append(
          self.put_artifact(
              artifact_type='DummyArtifact',
              properties={
                  'span': span,
                  'version': version
              }))

    for mlmd_artifact in mlmd_artifacts:
      self.put_execution(
          'ProducerNode',
          inputs={},
          outputs={'x': [mlmd_artifact]},
          contexts=[mlmd_context])

    return mlmd_artifacts

  def _insert_trainer_execution(
      self,
      model: artifact.Artifact,
      mlmd_context: metadata_store_pb2.Context,
  ):
    """Inserts Execution for a TFTrainer that outputs the Model artifact."""
    self.put_execution(
        'TFTrainer',
        # Note we intentionally do not take any input Examples artifacts, just
        # for testing.
        inputs={},
        outputs={'model': self.unwrap_tfx_artifacts([model])},
        contexts=[mlmd_context],
    )

  def assertResolvedAndMLMDArtifactListEqual(
      self,
      resolved_artifacts: metadata_store_pb2.Artifact,
      mlmd_artifacts: metadata_store_pb2.Artifact,
      check_span_and_version: bool = True,
  ):
    """Checks that a list of MLMD artifacts and resolved artifacts are equal."""
    self.assertEqual(len(mlmd_artifacts), len(resolved_artifacts))
    for mlmd_artifact, resolved_artifact in zip(mlmd_artifacts,
                                                resolved_artifacts):
      self.assertResolvedAndMLMDArtifactEqual(
          resolved_artifact, mlmd_artifact, check_span_and_version
      )

  def testLatestCreatedResolverFn_E2E(self):
    channel = canned_resolver_functions.latest_created(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        n=2,
    )
    pipeline_node = _compile_inputs({'x': channel})

    # Populate the MLMD database with DummyArtifacts to test the input
    # resolution end to end.
    mlmd_context = self.put_context('pipeline', 'pipeline')
    mlmd_artifact_1 = self.put_artifact('DummyArtifact')
    mlmd_artifact_2 = self.put_artifact('DummyArtifact')
    mlmd_artifact_3 = self.put_artifact('DummyArtifact')

    for mlmd_artifact in [mlmd_artifact_1, mlmd_artifact_2, mlmd_artifact_3]:
      self.put_execution(
          'ProducerNode',
          inputs={},
          outputs={'x': [mlmd_artifact]},
          contexts=[mlmd_context])

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # Check that actual_artifacts = [mlmd_artifact_2, mlmd_artifact_3] because
    # those two artifacts are the latest artifacts and n=2.
    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [mlmd_artifact_2, mlmd_artifact_3]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts
    )

  def testLatestVersionFn_E2E(self):
    channel = canned_resolver_functions.latest_version(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        n=1,
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [0, 0, 0]
    versions = [0, 1, 2]
    mlmd_artifacts = self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # The resolved artifacts should have (span, version) tuples of:
    # [(0, 2)].
    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [mlmd_artifacts[2]]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts
    )

  def testStaticRangeResolverFn_E2E(self):
    channel = canned_resolver_functions.static_range(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        end_span_number=5,
        keep_all_versions=True,
        exclude_span_numbers=[2],
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [0, 1, 2, 3, 3, 5, 7, 10]
    versions = [0, 0, 0, 0, 3, 0, 0, 0]
    mlmd_artifacts = self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # The resolved artifacts should have (span, version) tuples of:
    # [(0, 0), (1, 0), (3, 0), (3, 3), (5, 0)].
    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [mlmd_artifacts[i] for i in [0, 1, 3, 4, 5]]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts
    )

  def testStaticRangeResolverFn_MinSpans_RaisesSkip(self):
    channel = canned_resolver_functions.static_range(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        start_span_number=0,
        end_span_number=5,
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [0, 1, 2, 3, 3, 5, 7, 10]
    versions = [0, 0, 0, 0, 3, 0, 0, 0]
    self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertEmpty(resolved)  # Empty resolution implies Skip.

  def testRollingRangeResolverFn_E2E(self):
    channel = canned_resolver_functions.rolling_range(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        start_span_number=3,
        num_spans=2,
        skip_num_recent_spans=1,
        keep_all_versions=True,
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [1, 2, 3, 3, 7, 8]
    versions = [0, 0, 1, 0, 1, 2]
    mlmd_artifacts = self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # The resolved artifacts should have (span, version) tuples of:
    # [(3, 0), (3, 1), (7, 1)].
    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [mlmd_artifacts[i] for i in [3, 2, 4]]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts, check_span_and_version=True
    )

  def testAllSpansResolverFn_E2E(self):
    channel = canned_resolver_functions.all_spans(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact)
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [0, 1, 2, 3, 3, 5, 7, 10]
    versions = [0, 0, 0, 0, 3, 0, 0, 0]
    mlmd_artifacts = self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [mlmd_artifacts[i] for i in [0, 1, 2, 4, 5, 6, 7]]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts
    )

  def testShuffleResolverFn_E2E(self):
    channel = canned_resolver_functions.shuffle(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact)
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [1, 2, 3, 4]
    versions = [0, 0, 0, 0]
    self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    actual_spans = sorted([
        r.mlmd_artifact.properties['span'].int_value for r in resolved[0]['x']
    ])
    self.assertListEqual(actual_spans, spans)

  def testLatestPipelineRunOutputsResolverFn(self):
    producer_pipeline = pipeline.Pipeline(
        outputs={
            'x': channel_utils.artifact_query(
                artifact_type=test_utils.DummyArtifact
            )
        },
        pipeline_name='producer-pipeline',
    )
    return_value = canned_resolver_functions.latest_pipeline_run_outputs(
        pipeline=producer_pipeline)

    self.assertIsInstance(return_value['x'], types.BaseChannel)
    self.assertEqual('producer-pipeline',
                     return_value['x'].output_node.kwargs['pipeline_name'])

  def testRollingRangeResolverFn_MinSpans_RaisesSkip(self):
    channel = canned_resolver_functions.rolling_range(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        start_span_number=3,
        num_spans=5,
        skip_num_recent_spans=1,
    )
    pipeline_node = _compile_inputs({'x': channel})

    spans = [1, 2, 3, 3, 7, 8]
    versions = [0, 0, 1, 0, 1, 2]
    self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertEmpty(resolved)  # Empty resolution implies Skip.

  def testSequentialRollingRangeResolverFn_E2E(self):
    xs = canned_resolver_functions.sequential_rolling_range(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        start_span_number=1,
        num_spans=3,
        skip_num_recent_spans=1,
        keep_all_versions=False,
        exclude_span_numbers=[5],
    )
    with for_each.ForEach(xs) as each_x:
      inputs = {'x': each_x}
    pipeline_node = _compile_inputs(inputs)

    spans = [1, 2, 3, 3, 4, 5, 7]
    versions = [0, 0, 1, 0, 0, 0]
    mlmd_artifacts = self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # The resolved artifacts should have (span, version) tuples of:
    # [(1, 0), (2, 0), (3, 1)], [(2, 0), (3, 1), (4,0)].
    expected_artifact_idxs = [[0, 1, 2], [1, 2, 4]]
    for i, artifacts in enumerate(resolved):
      actual_artifacts = [r.mlmd_artifact for r in artifacts['x']]
      expected_artifacts = [
          mlmd_artifacts[j] for j in expected_artifact_idxs[i]
      ]
      self.assertResolvedAndMLMDArtifactListEqual(
          actual_artifacts, expected_artifacts
      )

  def testSequentialRollingRangeResolverFn_E2E_SkipRaised(self):
    # The artifacts will only have consecutive spans from [1, 5] but
    # num_spans=10 so no artifacts will be returnd by the resolver_fn and
    # a Skip will be raised during input resolution.
    xs = canned_resolver_functions.sequential_rolling_range(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        start_span_number=1,
        num_spans=10,
        skip_num_recent_spans=0,
        keep_all_versions=False,
    )
    with for_each.ForEach(xs) as each_x:
      inputs = {'x': each_x}
    pipeline_node = _compile_inputs(inputs)

    spans = [1, 2, 3, 3, 4, 5]
    versions = [0, 0, 1, 0, 0, 0]
    _ = self._insert_artifacts_into_mlmd(spans, versions)

    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle)
    self.assertEmpty(resolved)  # Empty resolution implies Skip.

  def testTrainingRangeResolverFn_E2E(self):
    contexts = [self.put_context('pipeline', 'pipeline')]

    # Build Examples.
    spans = [0, 1, 2, 3]
    versions = [0, 0, 0, 0]
    examples = self.create_examples(zip(spans, versions))

    # Train a Model on spans [1, 2, 3].
    model = self.prepare_tfx_artifact(test_utils.Model)
    self.train_on_examples(model, examples[1:4], contexts=contexts)

    # Perform input resoution.
    channel = canned_resolver_functions.training_range(
        channel_utils.artifact_query(artifact_type=test_utils.Model)
    )
    pipeline_node = _compile_inputs({'x': channel})
    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle
    )
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # Test that the input resolution returns Examples with spans 1, 2, and 3.
    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [e.mlmd_artifact for e in examples[1:4]]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts
    )

  def testLatestTrainedResolverFn_E2E(self):
    model_1 = self.prepare_tfx_artifact(test_utils.Model)
    model_2 = self.prepare_tfx_artifact(test_utils.Model)
    model_3 = self.prepare_tfx_artifact(test_utils.Model)

    mlmd_context = self.put_context('pipeline', 'pipeline')
    for model in [model_1, model_2, model_3]:
      self._insert_trainer_execution(model, mlmd_context)

    channel = canned_resolver_functions.latest_trained(
        types.Channel(test_utils.Model),
    )
    pipeline_node = _compile_inputs({'x': channel})
    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle
    )
    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.

    # Test that the input resolution returns the latest Model.
    actual_artifacts = [r.mlmd_artifact for r in resolved[0]['x']]
    expected_artifacts = [model_3.mlmd_artifact]
    self.assertResolvedAndMLMDArtifactListEqual(
        actual_artifacts, expected_artifacts
    )

    # Test that the Inovocation is properly updated, since latest_trained()
    # wraps _latest_policy_model().
    self.assertIsInstance(channel, resolved_channel.ResolvedChannel)
    self.assertEqual(channel.invocation.function.__name__, 'latest_trained')
    self.assertEndsWith(
        channel.invocation.function.__module__, 'canned_resolver_functions'
    )
    self.assertLen(channel.invocation.args, 1)
    self.assertIsInstance(channel.invocation.args[0], types.Channel)
    self.assertEqual(
        channel.invocation.kwargs,
        {
            'raise_skip_signal': True,
        },
    )

  def testLatestTrainedResolverFn_RaiseSkipSignalTrue(self):
    channel = canned_resolver_functions.latest_trained(
        types.Channel(test_utils.Model)
    )
    pipeline_node = _compile_inputs({'x': channel})
    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle
    )
    self.assertEmpty(resolved)  # Empty resolution implies Skip.

  def testLatestTrainedResolverFn_RaiseSkipSignalFalse(self):
    channel = canned_resolver_functions.latest_trained(
        types.Channel(test_utils.Model), raise_skip_signal=False
    )
    pipeline_node = _compile_inputs({'x': channel})
    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle
    )

    self.assertNotEmpty(resolved)  # Non-empty resolution implies Trigger.
    self.assertEmpty(resolved[0]['x'])

  def testSpanDrivenEvaluatorsResolverFn_E2E(self):
    contexts = [self.put_context('pipeline', 'pipeline')]

    # Build Examples with spans [1, 2, ... , 10]
    spans = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    versions = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    examples = self.create_examples(zip(spans, versions), contexts=contexts)

    # Train 5 Models on 3 spans [N, N+1, N+2].
    models = []
    for i in range(5):
      model = self.prepare_tfx_artifact(test_utils.Model)
      self.train_on_examples(model, examples[i : i + 3], contexts=contexts)
      models.append(model)

    # Perform input resoution.
    channel_by_str = canned_resolver_functions.span_driven_evaluator_inputs(
        examples=channel_utils.artifact_query(
            artifact_type=test_utils.Examples
        ),
        models=channel_utils.artifact_query(artifact_type=test_utils.Model),
        wait_spans_before_eval=1,
        evaluation_training_offset=2,
        additional_spans_per_eval=3,
        start_span_number=1,
    )
    pipeline_node = _compile_inputs(channel_by_str)
    resolved = inputs_utils.resolve_input_artifacts(
        pipeline_node=pipeline_node, metadata_handler=self.mlmd_handle
    )

    self.assertNotEmpty(resolved)
    expected = {
        # The resolved Model should be trained on spans smaller than 6 - 2 = 4.
        'model': [models[0]],
        # The resolved Examples should have spans [10 - 1 - 3, 10 - 1] = [6, 9].
        'examples': examples[5:9],
    }
    self.assertArtifactMapsEqual(expected, resolved[0])

  def testResolverFnContext(self):
    channel = canned_resolver_functions.latest_created(
        channel_utils.artifact_query(artifact_type=test_utils.DummyArtifact),
        n=2,
    )

    self.assertIsInstance(channel, resolved_channel.ResolvedChannel)
    self.assertEqual(channel.invocation.function.__name__, 'latest_created')
    self.assertEndsWith(channel.invocation.function.__module__,
                        'canned_resolver_functions')
    self.assertTrue(hasattr(channel.invocation.function, '__signature__'))

    self.assertLen(channel.invocation.args, 1)
    self.assertIsInstance(channel.invocation.args[0], resolver_op.InputNode)

    self.assertEqual(channel.invocation.kwargs, {'n': 2})


if __name__ == '__main__':
  tf.test.main()
