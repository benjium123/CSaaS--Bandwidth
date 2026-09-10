/** P23a: Settings -> AI -> Assistants. The export keeps its old name because SettingsPage (and
 * its test) mount `AgentPage`; the builder itself lives in components/assistants so each tab can
 * be tested on its own. */
export { AssistantsBuilder as AgentPage } from "@/components/assistants/AssistantsBuilder";
