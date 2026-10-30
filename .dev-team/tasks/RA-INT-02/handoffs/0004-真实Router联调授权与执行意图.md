# 真实Router联调授权与执行意图

2026-10-29，总控。负责人对两条异步问题分别明确回复：

> 已恢复
>
> 批准恢复后执行最多1个新Task

来源为本会话send_user_message_question_reply，服务问题call_aca4a7aba7104fe98b7900c4f6e3b8be/0，
范围问题call_896b40bc04a6488e84fa89cbf2c1d768/0；原问题明确独立回环18782、专用测试库、
Redis13、原50条/5类合成数据、上海2026-10-01～20、真实Router及对方模型费用，最多1个
新远程Task，正式Manifest仍关闭、主Runtime和主库不变。这是本批实际新授权，不复用旧六Task。

候选输入原SHA为0a90fdba9c3b152c21809f2118943b55453ddacec98076253dc102c19b0a1423；
evidence/真实Router联调执行输入.json只补实际批准记录，inputs与候选逐项相同，操作键仍为
sha256:2eab0cf360d5c00873fe26777ca30607944f483e50e1d1aae3cec525d12b48d6。
四项代码/配置输入版本已新鲜核对一致。保存本意图后才允许专用库创建/正常setup、测试
用户三条allow、独立Runtime和唯一新请求；未知结果查同一请求，不换ID重做。

恢复后只读预检见evidence/恢复后执行预检.json：Card GET200、原始SHA与交付74f4db…相同，
当前SDK/Profile校验通过，接口仍为127.0.0.1:18080/api；API/Worker均running且镜像0f0c10…、
revision a74183f…匹配原交付。项目PG/Redis运行，18782可绑定，普通Router运行回执不存在。
这不是业务请求结果；创建库前仍先查同名库，若已存在只读核对而不删除重建。

固定批次29e46ecf-dcc4-5cf2-967f-f899f4e8b308，请求56a29c21-6f27-5427-9394-1fd829270989，
数据库agent_runtime_ra_int02_29e46ecf、固定测试用户与Redis13均按执行输入。总控先完成
固定库核查/创建，随后委派开发执行既有已审入口、记录Run/Invocation/Task和公开结果证据。
只跟踪该Run至实际终态；主环境、对方服务、源数据和幂等事实不动，不提交/推送。
必要取消仅限本Task且须确认远程终态；独立Runtime在本Task终态确认后正常关闭。

当前操作登记执行中，预期回执为evidence/普通Router运行回执.json；真实结果及独立审核
尚未产生，不能将执行授权记为负责人结果终验。原准备checkpoint与所有最终交付门禁保留。
