"""
踢人插件

提供智能踢人功能的群聊管理插件。

功能特性：
- 智能LLM判定：根据聊天内容智能判断是否需要踢人
- 模板化消息：支持自定义踢人提示消息
- 参数验证：完整的输入参数验证和错误处理
- 配置文件支持：所有设置可通过配置文件调整
- 权限管理：支持用户权限和群组权限控制

包含组件：
- 智能踢人Action - 基于LLM判断是否需要踢人（支持群组权限控制）
- 踢人命令Command - 手动执行踢人操作（支持用户权限控制）
"""

from typing import List, Tuple, Type, Optional
import random

from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_plugin import register_plugin
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.component_types import ComponentInfo, ActionActivationType, ChatMode
from src.plugin_system.base.config_types import ConfigField
from src.common.logger import get_logger
from src.plugin_system.apis import person_api, generator_api

logger = get_logger("remove_user_plugin")

# ===== Action组件 =====

class RemoveUserAction(BaseAction):
    """智能踢人Action - 基于LLM智能判断是否需要踢人"""

    focus_activation_type = ActionActivationType.LLM_JUDGE
    normal_activation_type = ActionActivationType.KEYWORD
    mode_enable = ChatMode.ALL
    parallel_action = True

    action_name = "remove_user"
    action_description = "智能踢人系统，基于LLM判断是否需要踢人"

    activation_keywords = ["踢人", "remove", "kick", "移出"]
    keyword_case_sensitive = False

    llm_judge_prompt = """
你是有温度的赛博群友，而非机械执法程序。踢人决策需综合聊天语境和群组氛围判断
判定是否需要使用踢人动作
踢人动作的严格条件：

使用踢人的情况：
1. 用户发送严重违规内容（色情、暴力、政治敏感等）
2. 恶意刷屏或垃圾信息轰炸
3. 用户主动明确要求被踢出群聊
4. 严重违反群规的行为
5. 恶意攻击他人或群组管理

绝对不要使用的情况：
2. 情绪化表达但无恶意
3. 开玩笑或调侃，除非过分
4. 单纯的意见分歧或争论
5. 对方的权限比你高或相同
"""

    action_parameters = {
        "target": "踢人对象，必填，输入你要踢出的对象的名字，请仔细思考不要弄错对象",
        "reason": "踢人理由，可选",
    }

    action_require = [
        "当有人严重违反群规时使用",
        "当有人发了擦边，或者色情内容时使用",
        "当有人要求踢出自己时使用",
        "如果某人已经被踢出群聊了，就不要再次操作",
    ]

    associated_types = ["text", "command"]

    def _check_group_permission(self) -> Tuple[bool, Optional[str]]:
        if not self.is_group:
            return False, "踢人动作只能在群聊中使用"
        allowed_groups = self.get_config("permissions.allowed_groups", [])
        if not allowed_groups:
            logger.info(f"{self.log_prefix} 群组权限未配置，允许所有群使用踢人动作")
            return True, None
        current_group_key = f"{self.platform}:{self.group_id}"
        for allowed_group in allowed_groups:
            if allowed_group == current_group_key:
                logger.info(f"{self.log_prefix} 群组 {current_group_key} 有踢人动作权限")
                return True, None
        logger.warning(f"{self.log_prefix} 群组 {current_group_key} 没有踢人动作权限")
        return False, "当前群组没有使用踢人动作的权限"

    async def execute(self) -> Tuple[bool, Optional[str]]:
        logger.info(f"{self.log_prefix} 执行智能踢人动作")
        has_permission, permission_error = self._check_group_permission()
        target = self.action_data.get("target")
        reason = self.action_data.get("reason", "违反群规")
        if not target:
            error_msg = "踢人目标不能为空"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self.send_text("没有指定踢人对象呢~")
            return False, error_msg
        person_id = person_api.get_person_id_by_name(target)
        user_id = await person_api.get_person_value(person_id, "user_id")
        if not user_id:
            error_msg = f"未找到用户 {target} 的ID"
            await self.send_text(f"找不到 {target} 这个人呢~")
            logger.error(f"{self.log_prefix} {error_msg}")
            return False, error_msg
        message = self._get_template_message(target, reason)
        if not has_permission:
            logger.warning(f"{self.log_prefix} 权限检查失败: {permission_error}")
            result_status, result_message = await generator_api.rewrite_reply(
                chat_stream=self.chat_stream,
                reply_data={
                    "raw_reply": "我想踢出{target}，但是我没有权限",
                    "reason": "表达自己没有在这个群踢人的能力",
                },
            )
            if result_status:
                for reply_seg in result_message:
                    data = reply_seg[1]
                    await self.send_text(data)
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"尝试踢出了用户 {target}，但是没有权限，无法操作",
                action_done=True,
            )
            return False, permission_error
        result_status, result_message = await generator_api.rewrite_reply(
            chat_stream=self.chat_stream,
            reply_data={
                "raw_reply": message,
                "reason": reason,
            },
        )
        if result_status:
            for reply_seg in result_message:
                data = reply_seg[1]
                await self.send_text(data)
        # 发送群聊踢人命令（使用 send_api）
        from src.plugin_system.apis import send_api
        group_id = self.group_id if hasattr(self, "group_id") else None
        platform = self.platform if hasattr(self, "platform") else "qq"
        if not group_id:
            error_msg = "无法获取群聊ID"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self.send_text("执行踢人动作失败（群ID缺失）")
            return False, error_msg
        success = await send_api.command_to_group(
            command="GROUP_REMOVE",
            group_id=group_id,
            platform=platform,
            storage_message=False,
            qq_id=str(user_id)
        )
        if success:
            logger.info(f"{self.log_prefix} 成功发送踢人命令，用户 {target}({user_id})")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"尝试踢出了用户 {target}，原因：{reason}",
                action_done=True,
            )
            return True, f"成功踢出 {target}"
        else:
            error_msg = "发送踢人命令失败"
            logger.error(f"{self.log_prefix} {error_msg}")
            await self.send_text("执行踢人动作失败")
            return False, error_msg

    def _get_template_message(self, target: str, reason: str) -> str:
        templates = self.get_config("remove.templates")
        template = random.choice(templates)
        return template.format(target=target, reason=reason)

# ===== Command组件 =====

class RemoveUserCommand(BaseCommand):
    """踢人命令 - 手动执行踢人操作"""
    command_name = "remove_user_command"
    command_description = "踢人命令，手动执行踢人操作"
    command_pattern = r"^/remove\s+(?P<target>\S+)(?:\s+(?P<reason>.+))?$"
    command_help = "踢出指定用户，用法：/remove <用户名> [理由]"
    command_examples = ["/remove 用户名", "/remove 张三 违规", "/remove @某人 违反群规"]
    intercept_message = True

    def _check_user_permission(self) -> Tuple[bool, Optional[str]]:
        chat_stream = self.message.chat_stream
        if not chat_stream:
            return False, "无法获取聊天流信息"
        current_platform = chat_stream.platform
        current_user_id = str(chat_stream.user_info.user_id)
        allowed_users = self.get_config("permissions.allowed_users", [])
        if not allowed_users:
            logger.info(f"{self.log_prefix} 用户权限未配置，允许所有用户使用踢人命令")
            return True, None
        current_user_key = f"{current_platform}:{current_user_id}"
        for allowed_user in allowed_users:
            if allowed_user == current_user_key:
                logger.info(f"{self.log_prefix} 用户 {current_user_key} 有踢人命令权限")
                return True, None
        logger.warning(f"{self.log_prefix} 用户 {current_user_key} 没有踢人命令权限")
        return False, "你没有使用踢人命令的权限"

    async def execute(self) -> Tuple[bool, Optional[str]]:
        try:
            has_permission, permission_error = self._check_user_permission()
            if not has_permission:
                logger.error(f"{self.log_prefix} 权限检查失败: {permission_error}")
                await self.send_text(f"❌ {permission_error}")
                return False, permission_error
            target = self.matched_groups.get("target")
            reason = self.matched_groups.get("reason", "管理员操作")
            if not target:
                await self.send_text("❌ 命令参数不完整，请检查格式")
                return False, "参数不完整"
            person_id = person_api.get_person_id_by_name(target)
            user_id = await person_api.get_person_value(person_id, "user_id")
            if not user_id or user_id == "unknown":
                error_msg = f"未找到用户 {target} 的ID，请输入person_name进行踢人"
                await self.send_text(f"❌ 找不到用户 {target} 的ID，请输入person_name进行踢人，而不是qq号或者昵称")
                logger.error(f"{self.log_prefix} {error_msg}")
                return False, error_msg
            logger.info(f"{self.log_prefix} 执行踢人命令: {target}({user_id})")
            # 发送群聊踢人命令（使用 send_api）
            from src.plugin_system.apis import send_api
            group_id = self.message.chat_stream.group_info.group_id if self.message.chat_stream and self.message.chat_stream.group_info else None
            platform = self.message.chat_stream.platform if self.message.chat_stream else "qq"
            if not group_id:
                await self.send_text("❌ 无法获取群聊ID")
                return False, "群聊ID缺失"
            success = await send_api.command_to_group(
                command="GROUP_REMOVE",
                group_id=group_id,
                platform=platform,
                storage_message=False,
                qq_id=str(user_id)
            )
            if success:
                message = self._get_template_message(target, reason)
                await self.send_text(message)
                logger.info(f"{self.log_prefix} 成功踢出 {target}({user_id})")
                return True, f"成功踢出 {target}"
            else:
                await self.send_text("❌ 发送踢人命令失败")
                return False, "发送踢人命令失败"
        except Exception as e:
            logger.error(f"{self.log_prefix} 踢人命令执行失败: {e}")
            await self.send_text(f"❌ 踢人命令错误: {str(e)}")
            return False, str(e)

    def _get_template_message(self, target: str, reason: str) -> str:
        templates = self.get_config("remove.templates")
        template = random.choice(templates)
        return template.format(target=target, reason=reason)

# ===== 插件主类 =====

@register_plugin
class RemoveUserPlugin(BasePlugin):
    """踢人插件
    提供智能踢人功能：
    - 智能踢人Action：基于LLM判断是否需要踢人（支持群组权限控制）
    - 踢人命令Command：手动执行踢人操作（支持用户权限控制）
    """
    plugin_name = "remove_user_plugin"
    enable_plugin = True
    config_file_name = "config.toml"
    config_section_descriptions = {
        "plugin": "插件基本信息配置",
        "components": "组件启用控制",
        "permissions": "权限管理配置",
        "remove": "核心踢人功能配置",
        "smart_remove": "智能踢人Action的专属配置",
        "remove_command": "踢人命令Command的专属配置",
        "logging": "日志记录相关配置",
    }
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="0.0.1", description="配置文件版本"),
        },
        "components": {
            "enable_smart_remove": ConfigField(type=bool, default=True, description="是否启用智能踢人Action"),
            "enable_remove_command": ConfigField(
                type=bool, default=False, description="是否启用踢人命令Command（调试用）"
            ),
        },
        "permissions": {
            "allowed_users": ConfigField(
                type=list,
                default=[],
                description="允许使用踢人命令的用户列表，格式：['platform:user_id']，如['qq:123456789']。空列表表示不启用权限控制",
            ),
            "allowed_groups": ConfigField(
                type=list,
                default=[],
                description="允许使用踢人动作的群组列表，格式：['platform:group_id']，如['qq:987654321']。空列表表示不启用权限控制",
            ),
        },
        "remove": {
            "enable_message_formatting": ConfigField(
                type=bool, default=True, description="是否启用人性化的消息显示"
            ),
            "log_remove_history": ConfigField(type=bool, default=True, description="是否记录踢人历史（未来功能）"),
            "templates": ConfigField(
                type=list,
                default=[
                    "好的，已将 {target} 移出群聊，理由：{reason}",
                    "收到，对 {target} 执行踢人操作，因为{reason}",
                    "明白了，移除 {target}，原因是{reason}",
                    "哇哈哈哈哈哈，已将 {target} 踢出群聊，理由：{reason}",
                    "哎呦我去，对 {target} 执行踢人操作，因为{reason}",
                    "{target}，你完蛋了，我要把你踢出群聊，原因：{reason}",
                ],
                description="成功踢人后发送的随机消息模板",
            ),
            "error_messages": ConfigField(
                type=list,
                default=[
                    "没有指定踢人对象呢~",
                    "找不到 {target} 这个人呢~",
                    "查找用户信息时出现问题~",
                ],
                description="执行踢人过程中发生错误时发送的随机消息模板",
            ),
        },
        "smart_remove": {
            "strict_mode": ConfigField(type=bool, default=True, description="LLM判定的严格模式"),
            "keyword_sensitivity": ConfigField(
                type=str, default="normal", description="关键词激活的敏感度", choices=["low", "normal", "high"]
            ),
            "allow_parallel": ConfigField(type=bool, default=False, description="是否允许并行执行（暂未启用）"),
        },
        "remove_command": {
            "max_batch_size": ConfigField(type=int, default=5, description="最大批量踢人数量（未来功能）"),
            "cooldown_seconds": ConfigField(type=int, default=3, description="命令冷却时间（秒）"),
        },
        "logging": {
            "level": ConfigField(
                type=str, default="INFO", description="日志记录级别", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
            ),
            "prefix": ConfigField(type=str, default="[RemoveUserPlugin]", description="日志记录前缀"),
            "include_user_info": ConfigField(type=bool, default=True, description="日志中是否包含用户信息"),
            "include_action_info": ConfigField(type=bool, default=True, description="日志中是否包含操作信息"),
        },
    }
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        enable_smart_remove = self.get_config("components.enable_smart_remove", True)
        enable_remove_command = self.get_config("components.enable_remove_command", True)
        components = []
        if enable_smart_remove:
            components.append((RemoveUserAction.get_action_info(), RemoveUserAction))
        if enable_remove_command:
            components.append((RemoveUserCommand.get_command_info(), RemoveUserCommand))
        return components
