「再见」收播画面样本（算作 normal）

本目录：train/normal 41 张，val/normal 11 张。
画面是蓝底抽象背景加竖排「再见」，是收播正常画面，不是蓝屏故障。
不要放进 blue_screen，也不要放进故障类。

已同步到 GPU 机：
Z:\ai_monitor_code-main\dataset_frame\{train,val}\normal\zaijian_*

监测进程默认会在 AI 最高类为 normal、分数不低于 0.7、结果不超过 30 秒时，不报静帧和无伴音。
这些图留给下一次训练，让模型自己把「再见」认成 normal。
输出目录用 training\runs\frame_zaijian1，不要覆盖已经训过的 frame1。
