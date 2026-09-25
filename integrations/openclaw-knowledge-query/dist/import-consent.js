const PREFIX = "(?:请|请你|麻烦|帮我|请帮我)?(?:把|将)?(?:这(?:个|份)?(?:文件|附件|资料|文档|pdf)?|它)?";
const ACTION = "(?:放进|放到|放入|放|加入|加到|导入|存入|存到|保存到|收录到)";
const PRIVATE_CHINESE = new RegExp(`^${PREFIX}${ACTION}(?:我的)?(?:私人知识库|私有知识库|个人知识库|私库)[。！!]?$`, "u");
const SHARED_CHINESE = new RegExp(`^${PREFIX}${ACTION}(?:家庭共享知识库|家庭共享库|家庭知识库|共享知识库|共享库)[。！!]?$`, "u");
/** Deliberately narrow: only a direct write instruction with an explicit knowledge-base destination. */
export function parseExplicitImportConsent(content) {
    if (typeof content !== "string" || content.length > 200 || /[\r\n]/u.test(content))
        return null;
    const text = content.normalize("NFKC").replace(/\s+/gu, "").toLowerCase();
    if (PRIVATE_CHINESE.test(text))
        return "private";
    if (SHARED_CHINESE.test(text))
        return "shared";
    if (/^(?:please)?(?:import|save|add)(?:this|the)?(?:attachment|file|document|pdf)?(?:to|into)(?:my)?privateknowledgebase[.!]?$/u.test(text))
        return "private";
    if (/^(?:please)?(?:import|save|add)(?:this|the)?(?:attachment|file|document|pdf)?(?:to|into)(?:householdshared|familyshared|family|shared)knowledgebase[.!]?$/u.test(text))
        return "shared";
    return null;
}
