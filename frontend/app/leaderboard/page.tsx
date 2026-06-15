import { redirect } from "next/navigation";

// The competition leaderboard was replaced by the shared-account model.
export default function Page() {
  redirect("/");
}
