/** What 安安 did, in the owner's words (owner report : a reply's line read 「本次回复用到的工具：master_list_orders
 * master_list_blockers」). One short phrase per tool master control has (master_tools.py MASTER_TOOLS; a python test
 * holds the two lists together), for the line under a reply, the step still running in the thinking bubble and the
 * one-line alert of an unknown outcome alike (the live activity strip left 总控, policy). The raw name stays
 * in a data attribute only.
 *
 * A reply records a call when it was STARTED, not when it went through (master_loop.py appends it before the handler
 * answers), so anything that changes or produces something is worded as what 安安 put in -- 「提交了……」 -- never as
 * done: whether it went through is the receipt's (or the card's) to say, right above the line. */
const WORDS: Readonly<Record<string, readonly [string, string]>> = {
  // reads
  master_list_orders: ['查了工单', 'checked the orders'],
  master_order_detail: ['看了一张单的详情', 'looked into one order'],
  master_list_blockers: ['查了等你处理的事', 'checked what waits on you'],
  master_capabilities: ['查了系统能做什么', 'checked what this system can do'],
  master_process_drafts: ['查了在搭的流程', 'checked the flows being built'],
  master_list_order_flows: ['查了能接单的业务流', 'checked the flows that take orders'],
  master_connector_candidates: ['查了连接器候选', 'checked the connector candidates'],
  master_improvement_proposals: ['查了改进建议', 'checked the improvement proposals'],
  master_list_files: ['看了文件清单', 'listed the files'],
  master_preflight_candidate: ['预检了一条候选流程', 'pre-checked a candidate flow'],
  master_read_file: ['读了一个文件', 'read a file'],
  master_search_components: ['找了能用的组件', 'searched for components'],
  // 总控施工模式 (policy, B1): the build jobs of this project.
  master_list_builds: ['查了施工任务', 'checked the build jobs'],
  // Reads that produce something (a card on the reply when it went through; a stale version or a bad table is refused).
  master_propose_candidate_nodes: ['提交了流程改法', 'submitted a change to a flow'],
  master_propose_flow_draft: ['提交了新流程草案', 'submitted a new flow draft'],
  master_resume_flow_draft: ['继续了流程草案', 'resumed a flow draft'],
  master_propose_component_candidate: ['提交了组件候选', 'submitted a component candidate'],
  // writes: set out to do, the receipt says whether it went through
  master_create_order: ['提交了起草工单', 'asked to draft a work order'],
  master_reply_checkpoint: ['提交了一项批准或驳回', 'asked to answer a waiting decision'],
  master_stop_order: ['提交了停单', 'asked to stop an order'],
  master_interrupt_turn: ['提交了叫停这一步', 'asked to interrupt a running step'],
  master_steer_turn: ['提交了给正在做的单补一句话', 'asked to send a running order a correction'],
  master_delegate_browser: ['提交了给一张单开浏览器', 'asked to give an order a browser'],
  master_snooze_blocker: ['提交了暂缓提醒', 'asked to hold a reminder'],
  master_run_offline_trial: ['提交了试跑流程', 'asked to run a flow offline'],
  master_generate_connector_candidate: ['提交了起草连接器候选', 'asked to draft a connector candidate'],
  master_create_improvement_proposal: ['提交了记一条改进建议', 'asked to record an improvement'],
  master_create_improvement_from_acceptance: ['提交了把客户打回记成改进', 'asked to turn a customer rejection into an improvement'],
  // 总控施工模式 (policy, B1): the build's own card says whether it started and how it goes.
  master_start_build: ['开了一个施工任务', 'started a build job'],
  master_continue_build: ['接着施工', 'continued a build'],
}

const UNKNOWN: readonly [string, string] = ['用了一项系统功能', 'used a system function']

/** The phrase for one tool; a name this list does not know is 「用了一项系统功能」, never the name itself. */
export function masterToolWords(name: string, zh: boolean): string {
  const known = Object.prototype.hasOwnProperty.call(WORDS, name) ? WORDS[name] : UNKNOWN
  return known[zh ? 0 : 1]
}

/** The tools this list has words for (the python side checks it against master_tools.py). */
export const WORDED_MASTER_TOOLS: readonly string[] = Object.keys(WORDS)
