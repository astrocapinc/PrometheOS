import { PageHeading } from "../components/ui";
import { Link } from "../router";

export function NotFoundPage() {
  return (
    <>
      <PageHeading title="Page not found" />
      <p>
        This address is not part of the hub. Go to <Link to="/now">Now</Link>.
      </p>
    </>
  );
}
