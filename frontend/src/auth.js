// Supabase Auth 接线。
//
// 设计跟后端对称：**没配就是本地模式，完全不启用登录。**
// 本地一键启动时这三个环境变量都不存在，isAuthEnabled 为 false，
// App 直接进主界面，行为跟以前一模一样。
// 部署到 Vercel 时配上 VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY，
// 登录才会生效。
//
// 这里用的是 anon key，它是**公开**的（会打进前端包里），这没问题——
// 它只允许调用 Supabase 的登录接口，不能读写任何数据。真正的门在后端：
// 后端用 JWT Secret（私密，只存在 Railway 的环境变量里）验签令牌。
import { createClient } from "@supabase/supabase-js";

const URL = import.meta.env.VITE_SUPABASE_URL;
const ANON = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const isAuthEnabled = Boolean(URL && ANON);

// 登录页在报「Invalid API key」时要把这两个显示出来。Supabase 那句报错
// 不说是哪个项目、也不说用了哪个 key，光看它没法判断到底是 URL 配错了、
// key 配错了、还是两个属于不同项目——只能一个个试。把实际用的值摆出来，
// 一眼就能跟 Supabase 后台对上。
// 两个都是公开值（本来就打包进前端），显示出来不泄露任何东西。
export const supabaseUrl = URL || null;
export const supabaseKeyHint = ANON ? `${ANON.slice(0, 12)}…${ANON.slice(-4)}` : null;

export const supabase = isAuthEnabled
  ? createClient(URL, ANON, {
      auth: {
        persistSession: true,      // 令牌存 localStorage，关掉浏览器再开还在
        autoRefreshToken: true,    // 令牌一小时过期，库会自动续期
      },
    })
  : null;

/** 当前会话的令牌。没启用认证或没登录时返回 null。 */
export async function getToken() {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data?.session?.access_token ?? null;
}

export async function signIn(email, password) {
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) throw new Error(error.message);
  return data;
}

export async function signUp(email, password) {
  const { data, error } = await supabase.auth.signUp({ email, password });
  if (error) throw new Error(error.message);
  // Supabase 默认开启邮箱确认。开着的时候这里拿不到 session，
  // 要用户先去邮箱点确认链接——必须把这件事明确告诉用户，
  // 否则界面会停在「注册成功但进不去」的状态，看起来像坏了。
  return { needsEmailConfirm: !data.session, data };
}

export async function signOut() {
  if (supabase) await supabase.auth.signOut();
}
