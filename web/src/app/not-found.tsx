import Link from "next/link";
import { Footer } from "@/components/Footer";
import { Nav } from "@/components/Nav";

export default function NotFound() {
  return (
    <>
      <Nav />
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
