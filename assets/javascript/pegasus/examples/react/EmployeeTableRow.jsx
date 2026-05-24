import React from "react";
import {Link} from "react-router-dom";

const EmployeeTableRow = function (props) {
  const formatter = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
  });
  return (
    <tr>
      <td>{props.name}</td>
      <td>{props.department}</td>
      <td className="text-right">{ formatter.format(props.salary) }</td>
      <td className="flex space-x-1 justify-end">
          <Link to={`edit/${props.id}`}>
            <div className="btn btn-outline">
              <span className="w-6 h-6 inline-flex justify-center items-center"><i className="fa fa-edit" /></span>
              <span className="hidden md:inline-block">Edit</span>
            </div>
          </Link>
          <a onClick={() => props.delete(props.index)}>
            <div className="btn btn-outline text-red-500 border-red-500 hover:bg-red-500 hover:border-red-700 hover:text-white">
              <span className="w-6 h-6 inline-flex justify-center items-center"><i className="fa fa-times" /></span>
              <span className="hidden md:inline-block">Delete</span>
            </div>
          </a>
      </td>
    </tr>
  );
}

export default EmployeeTableRow;
