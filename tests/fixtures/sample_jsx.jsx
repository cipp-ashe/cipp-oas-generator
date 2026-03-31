import React from "react";
const Users = () => {
    const { data } = ApiGetCall({
        url: "/api/ListGraphRequest?Endpoint=/users&$select=id,displayName&tenantFilter=" + tenant,
    });
    return <div>{JSON.stringify(data)}</div>;
};
const Groups = () => {
    const { data } = ApiGetCall({
        url: "/api/ListGraphRequest?Endpoint=/groups&tenantFilter=" + tenant,
    });
    return <div>{JSON.stringify(data)}</div>;
};
export { Users, Groups };
