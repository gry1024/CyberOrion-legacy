# Segmented interrupt/resume smoke

Validation-only artifact, not a performance result. The run used MiniMax-M3 and
source `18fef5b9dff8655901ca417c089a19a43258b698` before the final action-example
field fix. It was interrupted after `steps001-002`; `progress.json` reported one
observed, zero committed segments and no publication validity. Resume archived the
provisional file under `attempts/`, reran only the incomplete episode from seed
82702, and committed five segments covering all ten steps.

Use this directory only as evidence for atomic persistence and resume behavior.
