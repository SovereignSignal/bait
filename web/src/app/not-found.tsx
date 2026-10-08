import Link from "next/link";
import { Footer } from "@/components/Footer";

export default function NotFound() {
  return (
    <>
      <main className="col">
        <div className="lead">
          <h1>Not found</h1>
          <p>
            No such page. <Link href="/">Back to the feed</Link>.
          </p>
        </div>
        <Footer />
      </main>
    </>
  );
}
